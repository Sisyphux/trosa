"""「一笔」手绘图标生成物必须和生成器一致，且页面引用的图标都存在。"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


@unittest.skipUnless(shutil.which("node"), "需要 node 运行生成器")
class HandIconsTests(unittest.TestCase):
    def test_generated_files_are_current(self):
        result = subprocess.run(
            ["node", str(ROOT / "tools" / "hand_icons.mjs"), "--check"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_ui_icon_classes_used_by_the_app_have_a_mapping(self):
        css = (STATIC / "visual-v2.css").read_text(encoding="utf-8")
        mapped = set(re.findall(r"\.ui-icon-([a-z-]+) \{ --ui-icon:", css))
        sources = (STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r"ui-icon-([a-z][a-z-]*)", sources))
        used |= set(re.findall(r"uiIcon\('([a-z-]+)'", sources))
        used -= {"md", "id"}  # 档位修饰类，不是图标名
        self.assertEqual(sorted(n for n in used if n not in mapped and not n.endswith("-")), [])

    def test_index_html_hand_placeholders_are_filled(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for match in re.finditer(r'<svg[^>]*data-hand="([a-z-]+)"[^>]*>(.*?)</svg>', html, re.S):
            self.assertIn("<path", match.group(2), match.group(1))


if __name__ == "__main__":
    unittest.main()
