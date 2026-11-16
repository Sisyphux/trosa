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


@unittest.skipUnless(shutil.which('node'), 'node is required')
class WeeklyDisplayTextTests(unittest.TestCase):
    def test_only_known_metadata_is_hidden(self):
        js = (STATIC / 'app.js').read_text(encoding='utf-8')
        cases = [
            ('【客户背调 2026-11-12】\n正文 105529 元 2026-11-12 3mm 200张\n【/客户背调】',
             '\n正文 105529 元 2026-11-12 3mm 200张\n'),
            ('【客户背调】正文【/客户背调】 [ticket:105529]', '正文 '),
            ('<mark class="hl-green">正文</mark><br>下一步 [ticket:42]',
             '<mark class="hl-green">正文</mark><br>下一步 '),
            ('[订单:105529] [ticket:abc] 【客户背调 其他备注】 2026-11-20 12 件',
             '[订单:105529] [ticket:abc] 【客户背调 其他备注】 2026-11-20 12 件'),
            ('', ''), (None, ''),
        ]
        script = _function_source(js, 'weeklyDisplayText') + '\n' + (
            'const cases = ' + json.dumps(cases) + ';\n'
            'console.log(JSON.stringify(cases.map(([value]) => weeklyDisplayText(value))));'
        )
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [expected for _, expected in cases])


@unittest.skipUnless(shutil.which('node'), 'node is required')
class WeeklyProfileRenderTests(unittest.TestCase):
    def test_actual_profile_render_paths_preserve_raw_data(self):
        js = (STATIC / 'app.js').read_text(encoding='utf-8')
        names = ['escapeHtml', 'renderRichText', 'richPlainText', 'weeklyDisplayText',
                 'weeklyRecordHtml', 'weeklyDossierHeadHtml', 'refreshDossierTranslation']
        # Load any new display helper as well, while exercising production renderers.
        if 'function weeklyProfileText(' in js:
            names.append('weeklyProfileText')
        script = "const {JSDOM} = require('./browser-extension/node_modules/jsdom'); const document = new JSDOM('').window.document;\n"
        script += "var _RICH_TEXT_ALLOWED_COLORS = {yellow:1,green:1,pink:1};\n"
        script += '\n'.join(_function_source(js, name) for name in names)
        script += r"""
const assert = require('node:assert/strict');
const raw = '【客户背调 2026-11-12】\n<mark class="hl-green">Acrylic importer</mark><br>Delivery 2026-12-20, 200 sheets [订单:42] [ticket:42]\n【/客户背调】';
const zh = '【客户背调 2026-11-12】亚克力进口商，2026-12-20 交货，200 张 [ticket:42]【/客户背调】';
function weeklyDateLabel(v){return v || '';}
function weeklyCustomerFacts(){return '';}
function weeklyCustomerText(){return '';}
function weeklyTextLines(){return '';}
function weeklyDossierMeta(){return '';}
function customerSourceLabel(){return '';}
function formatDate(v){return v;}
function verify(el, translated, label) {
  assert.ok(!/客户背调|ticket:|2026-11-12|<mark|<br/.test(el.textContent + el.title), label + ': internal metadata leaked');
  assert.ok(el.textContent.includes(translated ? '亚克力进口商' : 'Acrylic importer'), label + ': profile lost');
  assert.ok(el.textContent.includes('2026-12-20'), label + ': business date lost');
  if(translated) assert.ok(el.title.includes('Acrylic importer') && el.title.includes('[订单:42]'), label + ': original title lost');
}
(async()=>{
  for (const translated of [false,true]) {
    const r = {customer_id:42,customer_company:'Acrylic Supply',customer_profile:raw,customer_profile_zh:translated?zh:''};
    const c = {company:r.customer_company,profile:raw,profile_zh:r.customer_profile_zh};
    const snapshot = JSON.stringify([r,c]);
    const holder = document.createElement('div');
    holder.innerHTML = weeklyRecordHtml(r,'hamid');
    verify(holder.querySelector('.wk-record-profile'),translated,'list');
    holder.innerHTML = weeklyDossierHeadHtml(c);
    verify(holder.querySelector('[data-dossier-profile]'),translated,'detail');
    assert.equal(JSON.stringify([r,c]),snapshot);
  }
  document.body.innerHTML='<div id="ovDetailModal"><p data-dossier-profile></p></div>';
  const cu={profile:raw,profile_zh:zh,translation_pending:false};
  const snapshot=JSON.stringify(cu);
  global.OV={_detailToken:1}; global.getWeekStart=()=>''; global.overviewWeekOffset=0;
  global.api=async()=>({customer:cu}); global.setTimeout=fn=>{global.pending=fn();};
  refreshDossierTranslation(42,'hamid',1,1,0); await global.pending;
  verify(document.querySelector('[data-dossier-profile]'),true,'delayed translation');
  assert.equal(JSON.stringify(cu),snapshot);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class WeeklyStyleSourceTests(unittest.TestCase):
    def test_retired_weekly_rules_do_not_return_to_legacy_layers(self):
        """The weekly page is styled once, in visual-v5.css section 16 (.wk-*)."""
        for name in ('style.css', 'visual-v2.css', 'visual-v3.css', 'visual-v4.css'):
            css = (STATIC / name).read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'\.weekly-(person|work|member|board|team)', css), name)
            self.assertNotIn('#page-overview', css, name)
        v5 = (STATIC / 'visual-v5.css').read_text(encoding='utf-8')
        for selector in ('.wk-person', '.wk-record', '.wk-person-name', '.wk-spec', '.wk-key', '.wk-empty', '.wk-error'):
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
        for token in ('wk-week-switch', 'wk-reports'):
            self.assertIn(token, html)
        for token in ('wk-week-current', 'wk-person', 'wk-record', 'wk-board', 'wk-spec', 'wk-key', 'is-lit'):
            self.assertIn(token, js)
        self.assertNotIn('weekly-work-card', js)
        self.assertNotIn('ov-week-nav', html)


if __name__ == '__main__':
    unittest.main()
