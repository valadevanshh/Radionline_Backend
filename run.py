import os
import sys

# Ensure repo root is on sys.path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# pyrefly: ignore [missing-import]
import uvicorn

if __name__ == "__main__":
    print("Starting RadioNet PACS FastAPI Backend Server on http://127.0.0.1:8000 ...")
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[os.path.join(project_root, "app")],
    )
