"""The weekly page (本周工作): week boundaries and the single style source."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app' / 'static'


def _function_source(source: str, name: str) -> str:
    start = source.index(f'function {name}(')
    depth = 0
    for index in range(source.index('{', start), len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f'unterminated function {name}')


@unittest.skipUnless(shutil.which('node'), 'node is required')
class WeeklyWeekBoundaryTests(unittest.TestCase):
    def _week_starts(self, now_iso: str, offset: int, tz: str) -> str:
        js = (STATIC / 'app.js').read_text(encoding='utf-8')
        script = '\n'.join([
            _function_source(js, 'localDateString'),
            _function_source(js, 'getWeekStart'),
            f'const RealDate = Date; const fixed = new RealDate({json.dumps(now_iso)});',
            'global.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [fixed.getTime()])); } };',
            f'console.log(getWeekStart({offset}));',
        ])
        result = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, check=True,
            env={'TZ': tz, 'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'},
        )
        return result.stdout.strip()

    def test_week_start_is_monday_in_local_time_before_8am(self):
        # 2026-10-08 is a Thursday. 07:00 in UTC+8 is still 2026-10-07 23:00 UTC.
        self.assertEqual(self._week_starts('2026-10-08T07:00:00+08:00', 0, 'Asia/Shanghai'), '2026-10-05')
        self.assertEqual(self._week_starts('2026-10-08T07:00:00+08:00', -1, 'Asia/Shanghai'), '2026-09-28')
        self.assertEqual(self._week_starts('2026-10-12T00:30:00+08:00', 0, 'Asia/Shanghai'), '2026-10-12')

    def test_week_start_on_sunday_belongs_to_the_week_that_just_ended(self):
        self.assertEqual(self._week_starts('2026-10-11T23:30:00+08:00', 0, 'Asia/Shanghai'), '2026-10-05')


class WeeklyStyleSourceTests(unittest.TestCase):
    def test_retired_weekly_rules_do_not_return_to_legacy_layers(self):
        """The weekly page is styled once, in visual-v5.css section 16 (.wk-*)."""
        for name in ('style.css', 'visual-v2.css', 'visual-v3.css', 'visual-v4.css'):
            css = (STATIC / name).read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'\.weekly-(person|work|member|board|team)', css), name)
            self.assertNotIn('#page-overview', css, name)
        v5 = (STATIC / 'visual-v5.css').read_text(encoding='utf-8')
        for selector in ('.wk-row', '.wk-fact', '.wk-member', '.wk-empty', '.wk-error'):
            self.assertIn(selector, v5)

    def test_visual_v5_blocks_are_balanced_and_section_16_is_whole(self):
        v5 = (STATIC / 'visual-v5.css').read_text(encoding='utf-8')
        stripped = re.sub(r'/\*.*?\*/', '', v5, flags=re.S)
        self.assertEqual(stripped.count('{'), stripped.count('}'))
        self.assertEqual(v5.count('/*'), v5.count('*/'))
        self.assertRegex(v5, r'/\* =+\n   16 本周工作')

    def test_markup_and_renderer_share_the_wk_namespace(self):
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        js = (STATIC / 'app.js').read_text(encoding='utf-8')
        for token in ('wk-week-switch', 'wk-week-current', 'wk-reports'):
            self.assertIn(token, html)
        for token in ('wk-row', 'wk-facts', 'wk-members', 'wk-board'):
            self.assertIn(token, js)
        self.assertNotIn('weekly-work-card', js)
        self.assertNotIn('ov-week-nav', html)


if __name__ == '__main__':
    unittest.main()
