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
        st.write("Could not salvage any valid JSON from output.")
        return None, "Could not salvage any valid JSON from output.", False


load_dotenv()
api_url = os.getenv("AZURE_URL")

if not api_url or not api_url.strip():
    st.error("AZURE_URL is not set in your .env file or is empty. Please check your .env configuration.")
    st.stop()

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


def generate_script_v22(old_script, type):
    prompt = f'''
<|system|>
You are an excellent and helpful assistant that converts old Postman scripts from legacy format (v2.1.0) to the modern format (v2.2.0). Retain the version as v2.1.0 in the schema only. If the script is empty, leave it empty.

<|user|>
Convert the following Postman {type} script to modern syntax. Schema version should stay v2.1.0.

Instructions:
1. Do not change the logic or structure of the script.
2. If the script is empty, return an empty string.
3. Do not add extra sample code or usage examples or the words "javascript" or "js".
4. Keep the number of tests same as in the original script.
5. Keep in mind that there is no function like pm.response.json(...).has() and also **eval method should not be used at all**
6. **DO NOT** give me a script which would lead to a no tests found error in Postman.
7. Do not use pm.response in pre request scripts.
8. Preserve original test descriptions; do not reword test titles.

### Syntax changes:
- Replace `tests["..."] =` with `pm.test(...)`.
- Use `pm.expect(...)` instead of other assertions.
- Replace `responseBody` with `pm.response.json()`.
- Replace `responseCode.code` with `pm.response.code`.


### Global utilities:
- If a global function (e.g., funcUtils) is used:
  - It must be stored using:
    `pm.globals.set('funcUtilsExclusive', function loadFuncUtils() {{ return {{ ... }}; }} + ')()');`
  - Retrieve it using:
    ```js
    let funcUtilsString = pm.globals.get("funcUtilsExclusive");
    eval(funcUtilsString);
    ```
  - Do not add `loadFuncUtils()` or modify this structure.

### Validations:
- Wrap function calls in `try/catch` with checks like `typeof <fn> === 'function'`.
- If a global function is referenced but undefined, add a warning comment.
- Never remove or change variable names or test count.

### Output:
Return the converted test script **as plain JavaScript only**, no extra comments, explanations, or markdown.

{old_script}
'''
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=3200)
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

def convert_scripts_in_collection(obj, parent_listen=None):
    if isinstance(obj, dict):
        # Process collection-level or folder-level 'event' array
        if "event" in obj and isinstance(obj["event"], list):
            for event in obj["event"]:
                listen_type = event.get("listen", None)
                if "script" in event:
                    convert_scripts_in_collection(event, parent_listen=listen_type)
        # Process script at this level (if any)
        if "script" in obj and isinstance(obj["script"], dict) and "exec" in obj["script"]:
            value = obj["script"]
            old_exec = value["exec"]
            if isinstance(old_exec, list) and all(line.strip() == "" for line in old_exec):
                value["exec"] = []
            else:
                script_text = "\n".join(old_exec) if isinstance(old_exec, list) else str(old_exec)
                try:
                    script_type = parent_listen if parent_listen in ("prerequest", "test") else "test"
                    new_script = generate_script_v22(script_text, script_type)
                    cleaned_script = new_script.strip()
                    for prefix in ["javascript", "js"]:
                        if cleaned_script.lower().startswith(prefix):
                            cleaned_script = cleaned_script[len(prefix):].lstrip(':').lstrip('\n').lstrip()
                    def is_balanced(s):
                        stack = []
                        pairs = {')': '(', '}': '{', ']': '['}
                        for c in s:
                            if c in '({[':
                                stack.append(c)
                            elif c in ')}]':
                                if not stack or stack[-1] != pairs[c]:
                                    return False
                                stack.pop()
                        return not stack
                    if not cleaned_script or not is_balanced(cleaned_script):
                        value["exec"] = []
                    else:
                        value["exec"] = cleaned_script.splitlines()
                except Exception as e:
                    st.warning(f"Script conversion failed: {e}")
        # Always recurse into 'item' arrays (folders/requests)
        if "item" in obj and isinstance(obj["item"], list):
            for subitem in obj["item"]:
                convert_scripts_in_collection(subitem)
        # Also process any other dict/list values (for robustness)
        for key, value in obj.items():
            if key not in ("event", "script", "item"):
                convert_scripts_in_collection(value, parent_listen=parent_listen)
    elif isinstance(obj, list):
        for item in obj:
            convert_scripts_in_collection(item, parent_listen=parent_listen)


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
