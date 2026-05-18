import argparse
import base64
import csv
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError
import imagehash

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
RPC_ENDPOINTS = [
    "https://ethereum.publicnode.com",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
]
IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 LightCultLocalAudit/1.0"
})


def safe_print(msg):
    print(msg, flush=True)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def token_uri_call_data(token_id: int) -> str:
    # tokenURI(uint256) selector = 0xc87b56dd
    return "0xc87b56dd" + format(token_id, "064x")


def decode_abi_string(hex_result: str) -> str:
    if not hex_result or hex_result == "0x":
        raise ValueError("empty eth_call result")
    if hex_result.startswith("0x"):
        hex_result = hex_result[2:]
    data = bytes.fromhex(hex_result)
    if len(data) < 64:
        # Some non-standard contracts may return a raw bytes32-ish string.
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")
    offset = int.from_bytes(data[0:32], "big")
    if offset + 32 > len(data):
        return data.rstrip(b"\x00").decode("utf-8", errors="replace")
    length = int.from_bytes(data[offset:offset+32], "big")
    raw = data[offset+32:offset+32+length]
    return raw.decode("utf-8", errors="replace")


def eth_call_token_uri(contract: str, token_id: int) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": contract, "data": token_uri_call_data(token_id)},
            "latest"
        ],
    }
    errors = []
    for endpoint in RPC_ENDPOINTS:
        try:
            r = SESSION.post(endpoint, json=payload, timeout=25)
            r.raise_for_status()
            j = r.json()
            if "error" in j:
                errors.append(f"{endpoint}: {j['error']}")
                continue
            result = j.get("result")
            return decode_abi_string(result)
        except Exception as e:
            errors.append(f"{endpoint}: {e}")
    raise RuntimeError("All RPC endpoints failed: " + " | ".join(errors))


def data_uri_to_json(uri: str):
    # Handles data:application/json;base64,...
    if not uri.startswith("data:"):
        return None
    try:
        header, payload = uri.split(",", 1)
        if ";base64" in header:
            raw = base64.b64decode(payload)
        else:
            raw = requests.utils.unquote(payload).encode("utf-8")
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"Could not decode data URI metadata: {e}")


def normalize_ipfs(uri: str):
    if not uri:
        return []
    uri = uri.strip()
    if uri.startswith("ipfs://ipfs/"):
        path = uri[len("ipfs://ipfs/"):]
        return [gw + path for gw in IPFS_GATEWAYS]
    if uri.startswith("ipfs://"):
        path = uri[len("ipfs://"):]
        return [gw + path for gw in IPFS_GATEWAYS]
    return [uri]


def fetch_json_from_uri(uri: str):
    data = data_uri_to_json(uri)
    if data is not None:
        return data, uri
    errors = []
    for url in normalize_ipfs(uri):
        try:
            r = SESSION.get(url, timeout=30)
            r.raise_for_status()
            return r.json(), url
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("Could not fetch metadata JSON: " + " | ".join(errors))


def extension_from_response(url: str, content_type: str) -> str:
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        ext = mimetypes.guess_extension(ctype)
        if ext:
            if ext == ".jpe":
                return ".jpg"
            return ext
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in IMAGE_EXTS:
        return ext
    return ".jpg"


def download_file(uri: str, out_base_no_ext: Path):
    errors = []
    for url in normalize_ipfs(uri):
        try:
            with SESSION.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                ext = extension_from_response(url, r.headers.get("content-type", ""))
                out_path = out_base_no_ext.with_suffix(ext)
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                return out_path, url
        except Exception as e:
            errors.append(f"{url}: {e}")
    raise RuntimeError("Could not download image: " + " | ".join(errors))


def image_phash(path: Path):
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        # Convert to RGB to normalize modes/transparency.
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        return imagehash.phash(im)


def collect_images(folder: Path, max_images: int):
    paths = []
    for root, dirs, files in os.walk(folder):
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() in IMAGE_EXTS:
                paths.append(p)
    paths = sorted(paths, key=lambda p: str(p).lower())
    if max_images and max_images > 0:
        paths = paths[:max_images]
    return paths


def write_csv(path: Path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-images", required=True)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--start-token", type=int, required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--max-local-images", type=int, default=0)
    ap.add_argument("--high-distance", type=int, default=8)
    ap.add_argument("--review-distance", type=int, default=16)
    args = ap.parse_args()

    run_root = Path(args.run_root)
    ref_dir = run_root / "reference-images"
    meta_dir = run_root / "metadata-json"
    reports_dir = run_root / "reports"
    ensure_dir(ref_dir)
    ensure_dir(meta_dir)
    ensure_dir(reports_dir)

    local_folder = Path(args.local_images)
    metadata_rows = []
    failed_rows = []
    reference_hashes = []

    safe_print(f"Downloading reference tokens {args.start_token} through {args.start_token + args.count - 1}...")

    for token_id in range(args.start_token, args.start_token + args.count):
        token_str = str(token_id).zfill(5)
        try:
            token_uri = eth_call_token_uri(args.contract, token_id)
            metadata, resolved_meta_url = fetch_json_from_uri(token_uri)
            meta_path = meta_dir / f"token-{token_str}.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            image_uri = metadata.get("image") or metadata.get("image_url") or metadata.get("imageUrl")
            if not image_uri:
                raise RuntimeError("No image field found in metadata")

            out_base = ref_dir / f"Light-Cult-Crypto-Club-{token_str}"
            image_path, resolved_image_url = download_file(image_uri, out_base)
            h = image_phash(image_path)

            row = {
                "TokenID": token_id,
                "TokenIDPadded": token_str,
                "Name": metadata.get("name", ""),
                "Description": metadata.get("description", ""),
                "TokenURI": token_uri,
                "ResolvedMetadataURL": resolved_meta_url,
                "ImageURI": image_uri,
                "ResolvedImageURL": resolved_image_url,
                "ReferenceImagePath": str(image_path),
                "PHash": str(h),
                "OpenSeaURL": f"https://opensea.io/assets/ethereum/{args.contract}/{token_id}",
                "AttributesJSON": json.dumps(metadata.get("attributes", []), ensure_ascii=False),
            }
            metadata_rows.append(row)
            reference_hashes.append({"token_id": token_id, "token_str": token_str, "path": image_path, "hash": h, "name": row["Name"], "opensea": row["OpenSeaURL"]})
            safe_print(f"  OK token {token_id}")
            time.sleep(0.10)
        except Exception as e:
            failed_rows.append({"TokenID": token_id, "Error": str(e)})
            safe_print(f"  FAILED token {token_id}: {e}")

    write_csv(reports_dir / "light-cult-reference-metadata.csv", metadata_rows, [
        "TokenID", "TokenIDPadded", "Name", "Description", "TokenURI", "ResolvedMetadataURL",
        "ImageURI", "ResolvedImageURL", "ReferenceImagePath", "PHash", "OpenSeaURL", "AttributesJSON"
    ])
    write_csv(reports_dir / "light-cult-reference-failures.csv", failed_rows, ["TokenID", "Error"])

    if not reference_hashes:
        raise RuntimeError("No reference images were downloaded successfully, so matching cannot run.")

    local_images = collect_images(local_folder, args.max_local_images)
    safe_print(f"Scanning local images: {len(local_images)} file(s)")

    match_rows = []
    local_fail_rows = []

    for idx, local_path in enumerate(local_images, start=1):
        try:
            local_hash = image_phash(local_path)
            scored = []
            for ref in reference_hashes:
                distance = local_hash - ref["hash"]
                scored.append((distance, ref))
            scored.sort(key=lambda x: x[0])
            best_distance, best_ref = scored[0]
            second_distance, second_ref = scored[1] if len(scored) > 1 else ("", {"token_id": "", "token_str": ""})

            if best_distance <= args.high_distance and (second_distance == "" or int(second_distance) - int(best_distance) >= 3):
                confidence = "HIGH"
                note = "Strong visual match. Still review before renaming."
            elif best_distance <= args.review_distance:
                confidence = "REVIEW"
                note = "Possible match. Review visually."
            else:
                confidence = "NO MATCH IN TEST SET"
                note = "No close match among the small reference set used in this test."

            suggested = f"Light-Cult-Crypto-Club-{best_ref['token_str']}{local_path.suffix.lower()}" if best_ref else ""
            match_rows.append({
                "LocalFile": local_path.name,
                "LocalPath": str(local_path),
                "BestTokenID": best_ref["token_id"],
                "BestTokenIDPadded": best_ref["token_str"],
                "BestDistance": best_distance,
                "SecondBestTokenID": second_ref.get("token_id", ""),
                "SecondBestDistance": second_distance,
                "Confidence": confidence,
                "SuggestedFilename": suggested,
                "ReferenceImagePath": str(best_ref["path"]),
                "MetadataName": best_ref.get("name", ""),
                "OpenSeaURL": best_ref.get("opensea", ""),
                "Notes": note,
            })
        except (UnidentifiedImageError, OSError, Exception) as e:
            local_fail_rows.append({"LocalPath": str(local_path), "Error": str(e)})

        if idx % 100 == 0:
            safe_print(f"  compared {idx} local images...")

    write_csv(reports_dir / "light-cult-local-match-report.csv", match_rows, [
        "LocalFile", "LocalPath", "BestTokenID", "BestTokenIDPadded", "BestDistance",
        "SecondBestTokenID", "SecondBestDistance", "Confidence", "SuggestedFilename",
        "ReferenceImagePath", "MetadataName", "OpenSeaURL", "Notes"
    ])
    write_csv(reports_dir / "light-cult-local-image-read-failures.csv", local_fail_rows, ["LocalPath", "Error"])

    summary = {
        "run_root": str(run_root),
        "contract": args.contract,
        "token_start": args.start_token,
        "token_count_requested": args.count,
        "reference_downloaded": len(metadata_rows),
        "reference_failures": len(failed_rows),
        "local_images_scanned": len(local_images),
        "high_confidence_matches": sum(1 for r in match_rows if r["Confidence"] == "HIGH"),
        "review_matches": sum(1 for r in match_rows if r["Confidence"] == "REVIEW"),
        "no_match_in_test_set": sum(1 for r in match_rows if r["Confidence"] == "NO MATCH IN TEST SET"),
        "local_read_failures": len(local_fail_rows),
        "main_report": str(reports_dir / "light-cult-local-match-report.csv"),
        "metadata_report": str(reports_dir / "light-cult-reference-metadata.csv"),
    }

    with open(reports_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(reports_dir / "READ-ME-FIRST.txt", "w", encoding="utf-8") as f:
        f.write("Light Cult image match test v1\n")
        f.write("\n")
        f.write("This was a safe test. It did not rename, move, or delete your original local images.\n")
        f.write("\n")
        for k, v in summary.items():
            f.write(f"{k}: {v}\n")
        f.write("\n")
        f.write("Open light-cult-local-match-report.csv in Excel and review the HIGH and REVIEW rows.\n")
        f.write("If many rows say NO MATCH IN TEST SET, that may simply mean the test reference range did not include those token IDs.\n")

    safe_print("\nDONE")
    safe_print(f"Main report: {reports_dir / 'light-cult-local-match-report.csv'}")
    safe_print(f"Summary: {reports_dir / 'READ-ME-FIRST.txt'}")


if __name__ == "__main__":
    main()
