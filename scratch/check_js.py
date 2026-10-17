import subprocess, os

edge_paths = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
]
edge = next((p for p in edge_paths if os.path.exists(p)), None)
if not edge:
    print("Edge not found")
    exit(0)

html_content = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>INITIAL</title></head>
<body>
<div id="log-container"></div>
<script src="test_script.js"></script>
<script>
window.addEventListener('error', function(e) {
    document.title = 'JS_ERROR: ' + e.message;
});
if (!document.title.startsWith('JS_ERROR')) {
    document.title = 'PASSED_NO_SYNTAX_ERROR';
}
</script>
</body>
</html>"""

with open("test_syntax.html", "w", encoding="utf-8") as f:
    f.write(html_content)

res = subprocess.run([edge, "--headless", "--dump-dom", "test_syntax.html"], capture_output=True, text=True, encoding="utf-8")
for line in res.stdout.splitlines():
    if "<title>" in line:
        print("Result:", line.strip())
