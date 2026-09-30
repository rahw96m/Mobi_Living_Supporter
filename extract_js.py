import re

from web_server import HTML_PAGE

m = re.findall(r'<script>(.*?)</script>', HTML_PAGE, re.DOTALL)
if not m:
    print("No script tag found!")
    exit(1)

js_code = m[0]
with open("extracted_script.js", "w", encoding="utf-8") as f:
    f.write(js_code)

print("Wrote extracted_script.js (length:", len(js_code), ")")
