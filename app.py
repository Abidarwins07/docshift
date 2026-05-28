import os
import uuid
import zipfile
import subprocess
import shutil
import tempfile
import time
import threading
from flask import Flask, request, send_file, jsonify, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MAX_FILE_SIZE    = 20 * 1024 * 1024   # 20 MB per file
MAX_FILES        = 10                  # max uploads per request
ALLOWED_EXTS     = {"docx", "doc"}
UPLOAD_FOLDER    = tempfile.gettempdir()
CLEANUP_DELAY    = 120                 # seconds before temp files are deleted

# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS


def is_valid_docx(path: str) -> bool:
    """Magic-byte check: DOCX/DOCM/DOTX are ZIP containers with Content_Types."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            return "[Content_Types].xml" in z.namelist()
    except Exception:
        return False


def is_valid_doc(path: str) -> bool:
    """Legacy .doc: first 8 bytes are the Compound Document File signature."""
    try:
        with open(path, "rb") as f:
            magic = f.read(8)
        return magic == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
    except Exception:
        return False


def validate_word_file(path: str, filename: str) -> bool:
    ext = filename.rsplit(".", 1)[1].lower()
    if ext == "docx":
        return is_valid_docx(path)
    elif ext == "doc":
        return is_valid_doc(path)
    return False


def convert_with_libreoffice(src: str, out_dir: str, timeout: int = 90) -> bool:
    """Run LibreOffice headless conversion. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--norestore",
                "--convert-to", "pdf",
                "--outdir", out_dir,
                src,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except FileNotFoundError:
        # LibreOffice not installed – try unoconv as fallback
        try:
            result = subprocess.run(
                ["unoconv", "-f", "pdf", "-o", out_dir, src],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0
        except Exception:
            return False


def delayed_cleanup(paths: list, delay: int = CLEANUP_DELAY):
    """Delete temp files after a delay in a background thread."""
    def _clean():
        time.sleep(delay)
        for p in paths:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
    threading.Thread(target=_clean, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/convert", methods=["POST"])
def convert():
    files = request.files.getlist("files")

    # ── Basic validations ──────────────────────────────────────────────────────
    if not files or all(f.filename == "" for f in files):
        return jsonify({"error": "No files uploaded."}), 400

    if len(files) > MAX_FILES:
        return jsonify({"error": f"Maximum {MAX_FILES} files allowed per request."}), 400

    session_dir = os.path.join(UPLOAD_FOLDER, f"conv_{uuid.uuid4().hex}")
    os.makedirs(session_dir, exist_ok=True)

    results   = []   # {"name": str, "status": "ok"|"error", "message": str}
    pdf_paths = []   # absolute paths to generated PDFs

    for f in files:
        original_name = secure_filename(f.filename)
        if not original_name:
            results.append({"name": f.filename, "status": "error",
                             "message": "Invalid filename."})
            continue

        if not allowed_file(original_name):
            results.append({"name": original_name, "status": "error",
                             "message": "Only .docx and .doc files are accepted."})
            continue

        # Check declared content-length (optional header)
        content_length = request.content_length
        if content_length and content_length > MAX_FILE_SIZE * MAX_FILES:
            results.append({"name": original_name, "status": "error",
                             "message": "Total upload exceeds size limit."})
            continue

        save_path = os.path.join(session_dir, original_name)
        f.save(save_path)

        # Hard size check after saving
        if os.path.getsize(save_path) > MAX_FILE_SIZE:
            os.remove(save_path)
            results.append({"name": original_name, "status": "error",
                             "message": "File exceeds 20 MB limit."})
            continue

        # Magic-byte validation
        if not validate_word_file(save_path, original_name):
            os.remove(save_path)
            results.append({"name": original_name, "status": "error",
                             "message": "File does not appear to be a valid Word document."})
            continue

        # ── Convert ────────────────────────────────────────────────────────────
        ok = convert_with_libreoffice(save_path, session_dir)
        if not ok:
            results.append({"name": original_name, "status": "error",
                             "message": "Conversion failed. Please try again."})
            continue

        stem     = os.path.splitext(original_name)[0]
        pdf_file = os.path.join(session_dir, f"{stem}.pdf")
        if not os.path.isfile(pdf_file):
            results.append({"name": original_name, "status": "error",
                             "message": "PDF output not found after conversion."})
            continue

        pdf_paths.append(pdf_file)
        results.append({"name": original_name, "status": "ok",
                         "message": f"{stem}.pdf ready"})

    if not pdf_paths:
        delayed_cleanup([session_dir])
        return jsonify({"error": "No files were converted successfully.", "results": results}), 422

    # ── Deliver ────────────────────────────────────────────────────────────────
    if len(pdf_paths) == 1:
        pdf = pdf_paths[0]
        delayed_cleanup([session_dir])
        return send_file(pdf, as_attachment=True,
                         download_name=os.path.basename(pdf),
                         mimetype="application/pdf")

    # Multiple PDFs → zip
    zip_path = os.path.join(UPLOAD_FOLDER, f"converted_{uuid.uuid4().hex[:8]}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pdf_paths:
            zf.write(p, arcname=os.path.basename(p))

    delayed_cleanup([session_dir, zip_path])
    return send_file(zip_path, as_attachment=True,
                     download_name="converted_pdfs.zip",
                     mimetype="application/zip")


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
