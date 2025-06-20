import os
import json
import zipfile
import tempfile
import requests
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
from io import BytesIO
from jsonschema import validate, ValidationError

load_dotenv()
api_url = os.getenv("AZURE_URL")

# Load Postman v2.2.0 JSON schema for validation
schema = None
try:
    schema_url = "https://schema.getpostman.com/json/collection/v2.2.0/collection.json"
    response = requests.get(schema_url, timeout=10)
    response.raise_for_status()
    schema = response.json()
except Exception as e:
    try:
        with open("postman_collection_v2.2_schema.json", "r", encoding="utf-8") as f:
            schema = json.load(f)
        st.warning("Loaded Postman v2.2 schema from local fallback.")
    except Exception as fallback_error:
        st.error(
            f"Failed to load Postman schema from both URL and local file.\n\n"
            f"URL error: {e}\nLocal error: {fallback_error}"
        )
        st.stop()

st.set_page_config(page_title="Postman Bulk Converter")
st.title("Convert All Postman JSONs from a Zipped Folder")

uploaded_zip = st.file_uploader("Upload a zipped folder of Postman collections (.zip)", type="zip")

def generate_postman_v22(oldpm_raw):
    prompt = f"""
<|system|>
You are a helpful assistant that converts old Postman test scripts from legacy format (v2.1.0) to the modern format (v2.2.0).

<|user|>
Convert the following old Postman JSON (v2.1.0) to the updated Postman v2.2.0 format.

Make the following changes:
- Convert old-style test assertions like `tests[\"name\"] = ...` to the newer `pm.test(...)` format.
- Replace boolean-based expressions (e.g., `tests[\"Status code is 200\"] = responseCode.code === 200`) with BDD-style assertions using `pm.test(...)` and `pm.expect(...)`.
- Use the `pm.response` object instead of legacy variables like `responseBody` or `responseCode`.
  - Replace `responseBody` with `pm.response.json()`.
  - Replace `responseCode.code` with `pm.response.code`.
- Example transformation:
  - From: `tests[\"Status code is 200\"] = responseCode.code === 200;`
  - To: `pm.test(\"Status code is 200\", function () {{ pm.expect(pm.response.code).to.eql(200); }});`

Output only valid Postman v2.2.0 JSON content with updated script blocks.  
Do **not** include any extra text, markdown, or formatting.  
If the input is not a valid Postman JSON object, return an empty JSON object.

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
    newpm = response.text.strip()

    if newpm.startswith("```json"):
        newpm = newpm[7:]
    if newpm.endswith("```"):
        newpm = newpm[:-3]

    return newpm

if uploaded_zip:
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "uploaded.zip")
        with open(zip_path, "wb") as f:
            f.write(uploaded_zip.read())

        extract_dir = os.path.join(tmpdir, "unzipped")
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        st.success("Zip extracted. Starting conversion...")

        converted_dir = os.path.join(tmpdir, "converted")
        os.makedirs(converted_dir, exist_ok=True)
        converted_files = 0

        for root, _, files in os.walk(extract_dir):
            for file in files:
                if file.endswith(".json"):
                    input_path = os.path.join(root, file)
                    try:
                        with open(input_path, "r", encoding="utf-8") as f:
                            oldpm_raw = f.read()

                        # Try parsing and checking if already valid
                        try:
                            oldpm = json.loads(oldpm_raw)
                            validate(instance=oldpm, schema=schema)
                            st.info(f"{file} already conforms to v2.2.0. Skipping conversion.")
                            continue
                        except:
                            pass  # proceed to conversion

                        # First attempt
                        newpm = generate_postman_v22(oldpm_raw)

                        try:
                            parsed = json.loads(newpm)
                            validate(instance=parsed, schema=schema)
                            st.success(f"{file} is valid Postman v2.2 JSON")
                        except (json.JSONDecodeError, ValidationError) as ve:
                            st.warning(f"{file} failed initial validation. Retrying...")
                            try:
                                newpm = generate_postman_v22(oldpm_raw)
                                parsed = json.loads(newpm)
                                validate(instance=parsed, schema=schema)
                                st.success(f"{file} is valid Postman v2.2 JSON after retry")
                            except Exception as retry_error:
                                st.warning(f"{file} is still invalid after retry:\n{retry_error}")
                                continue  # skip saving

                        # Save the converted file
                        base_name = Path(file).stem
                        output_path = os.path.join(converted_dir, f"{base_name}_converted.json")
                        with open(output_path, "w", encoding="utf-8") as out_f:
                            out_f.write(newpm)

                        converted_files += 1

                    except Exception as e:
                        st.error(f"Failed to convert {file}: {e}")

        if converted_files == 0:
            st.warning("No valid .json files were converted.")
        else:
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                for file in os.listdir(converted_dir):
                    file_path = os.path.join(converted_dir, file)
                    zipf.write(file_path, arcname=file)
            zip_buffer.seek(0)

            st.markdown("---")
            st.success(f"Converted {converted_files} file(s).")

            st.download_button(
                label="Download the converted Postman ZIP file.",
                data=zip_buffer,
                file_name="converted_postman_jsons.zip",
                mime="application/zip"
            )
