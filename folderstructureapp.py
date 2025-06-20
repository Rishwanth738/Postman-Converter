import os
import json
import zipfile
import tempfile
import requests
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
from io import BytesIO
from jsonschema import validate


def validate_as_v22_but_save_as_v21(obj):
    if "info" not in obj:
        obj["info"] = {}
    obj["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.2.0/collection.json"
    validate(instance=obj, schema=schema)
    obj["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def salvage_partial_json(raw_str):
    start = raw_str.find('{')
    end = raw_str.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None, "No JSON object boundaries found.", False
    candidate = raw_str[start:end+1]
    try:
        decoder = json.JSONDecoder()
        obj, idx = decoder.raw_decode(candidate)
        if idx < len(candidate):
            return obj, "Partial JSON salvaged from truncated output.", True
        return obj, None, False
    except Exception:
        for i in range(len(candidate), 0, -1):
            try:
                parsed = json.loads(candidate[:i])
                return parsed, "Partial JSON salvaged from truncated output.", i < len(candidate)
            except Exception:
                continue
    open_braces = candidate.count('{') - candidate.count('}')
    open_brackets = candidate.count('[') - candidate.count(']')
    fixed = candidate + ('}' * open_braces) + (']' * open_brackets)
    try:
        return json.loads(fixed), "Partial JSON salvaged by auto-closing braces/brackets.", True
    except Exception:
        return None, "Could not salvage any valid JSON from output.", False


load_dotenv()
api_url = os.getenv("AZURE_URL")

schema = None
try:
    with open("postman_collection_v2.2_schema.json", "r", encoding="utf-8") as f:
        schema = json.load(f)
    st.warning("Loaded Postman v2.2 schema from local fallback.")
except Exception as fallback_error:
    st.error(f"Failed to load Postman schema.\n\nError: {fallback_error}")
    st.stop()

st.set_page_config(page_title="Postman Bulk Converter")
st.title("Convert All Postman JSONs from a Zipped Folder")

uploaded_zip = st.file_uploader("Upload a zipped folder of Postman collections (.zip)", type="zip")


def generate_script_v22(old_script):
    prompt = f"""
<|system|>
You are a helpful assistant that converts old Postman test scripts from legacy format (v2.1.0) to the modern format (v2.2.0).

<|user|>
Convert the following old Postman test script to the updated Postman v2.2.0 format. But retain the version as v2.1.0 in the schema only.

Make the following changes:
- Replace tests["..."] = with pm.test(...)
- Use pm.expect(...) assertions
- Replace responseBody with pm.response.json()
- Replace responseCode.code with pm.response.code

Output only the converted script as plain JS.

{old_script}
"""
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=1600)
    response.raise_for_status()
    return response.text.strip().strip('`\n"\' ')

def generate_postman_v22_again(oldpm_raw):
    prompt = f"""
<|system|>
You are a helpful assistant that corrects the format of the Postman v2.2.0 collection.

<|user|>
Update this collection to Postman v2.2.0 with proper test scripts (pm.test, pm.expect, pm.response). Retain v2.1.0 in the schema string.

```json
{oldpm_raw}
"""
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=180)
    response.raise_for_status()
    fixed = response.text.strip().removeprefix("```json").removesuffix("```").strip()
    return fixed

def convert_scripts_in_collection(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "script" and isinstance(value, dict) and "exec" in value:
                old_exec = value["exec"]
                script_text = "\n".join(old_exec) if isinstance(old_exec, list) else str(old_exec)
                try:
                    new_script = generate_script_v22(script_text)
                    value["exec"] = new_script.splitlines() if new_script else []
                except Exception as e:
                    st.warning(f"Script conversion failed: {e}")
            else:
                convert_scripts_in_collection(value)
    elif isinstance(obj, list):
        for item in obj:
            convert_scripts_in_collection(item)


if uploaded_zip:
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "uploaded.zip")
        with open(zip_path, "wb") as f:
            f.write(uploaded_zip.read())

        extract_dir = os.path.join(tmpdir, "unzipped")
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        st.success("Zip extracted. Starting script conversion...")

        converted_dir = os.path.join(tmpdir, "converted")
        os.makedirs(converted_dir, exist_ok=True)
        converted_files = 0

        for root, _, files in os.walk(extract_dir):
            for file in files:
                if file.endswith(".json"):
                    input_path = os.path.join(root, file)
                    try:
                        with open(input_path, "r", encoding="utf-8") as f:
                            raw = f.read()
                        collection_json = json.loads(raw)

                        if "item" not in collection_json:
                            st.warning(f"{file} skipped: No 'item' key found.")
                            continue

                        convert_scripts_in_collection(collection_json)
                        validate_as_v22_but_save_as_v21(collection_json)

                        out_path = os.path.join(converted_dir, f"{Path(file).stem}_converted.json")
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(json.dumps(collection_json, indent=2))
                        converted_files += 1
                    except Exception as e:
                        st.error(f"Failed to process {file}: {e}")

        if converted_files == 0:
            st.warning("No valid .json files were converted.")
        else:
            valid_files = []
            invalid_files = []
            for file in os.listdir(converted_dir):
                file_path = os.path.join(converted_dir, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    validate_as_v22_but_save_as_v21(data)
                    valid_files.append(file)
                except Exception as e:
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            raw_json = f.read()
                        fixed_json = generate_postman_v22_again(raw_json)
                        parsed = json.loads(fixed_json)
                        validate_as_v22_but_save_as_v21(parsed)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(json.dumps(parsed, indent=2))
                        valid_files.append(file)
                        st.info(f"{file} was fixed and validated on retry.")
                    except Exception as e2:
                        invalid_files.append((file, f"Initial error: {e}; Retry error: {e2}"))

            if not valid_files:
                st.error("No valid collections were generated.")
                for fname, err in invalid_files:
                    st.info(f"{fname}: {err}")
            else:
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for file in valid_files:
                        file_path = os.path.join(converted_dir, file)
                        zipf.write(file_path, arcname=file)
                zip_buffer.seek(0)

                st.markdown("---")
                st.success(f"Converted {len(valid_files)} collections successfully.")
                if invalid_files:
                    st.warning(f"{len(invalid_files)} file(s) had issues.")
                    for fname, err in invalid_files:
                        st.info(f"{fname}: {err}")
                st.download_button(
                    label="Download Converted Collections (.zip)",
                    data=zip_buffer,
                    file_name="converted_postman_jsons.zip",
                    mime="application/zip"
                )
