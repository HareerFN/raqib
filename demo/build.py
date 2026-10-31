"""Build demo/raqib_demo.html from demo/template.html and demo/demo_data.json.

    python demo/export.py && python demo/build.py
The page is self-contained (data inlined); React and htm load from public CDNs.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent

data = (HERE / "demo_data.json").read_text(encoding="utf-8").replace("</", "<\\/")
page = (HERE / "template.html").read_text(encoding="utf-8").replace("__DEMO_DATA__", data)
(HERE / "raqib_demo.html").write_bytes(page.encode("utf-8"))
print(f"demo/raqib_demo.html ({len(page) // 1024} KB)")
