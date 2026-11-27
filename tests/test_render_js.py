"""Kiểm tra phần JavaScript của trang bằng trình duyệt thật (Playwright). Tự bỏ qua nếu máy không có node/Playwright."""
import json
import shutil
import subprocess

import pytest

from pipeline import db
from pipeline.config import load_settings
from pipeline.deep import parse_article
from pipeline.models import Article
from pipeline.preview import build_data, render
from tests.test_deep import make_article

CHECK = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto('file://' + process.argv[1]); await p.waitForTimeout(400);
  const r = await p.evaluate(() => {
    const art = document.querySelector('details.article .art');
    return { strong: [...art.querySelectorAll('strong')].map(s => s.textContent),
             stars: art.textContent.includes('**'),
             cites: art.querySelectorAll('a.cite').length,
             headings: [...art.querySelectorAll('h3')].map(h => h.textContent),
             objectText: art.textContent.includes('[object') };
  });
  console.log(JSON.stringify({ ...r, errs })); await b.close();
})();
"""


def _have_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _have_playwright(), reason="cần node và Playwright")
def test_article_renders_bold_citations_and_sections(tmp_path):
    settings = load_settings()
    conn = db.connect(tmp_path / "t.db")
    db.insert_articles(conn, [("k", Article(url="https://vnexpress.net/a", title="Tin", source="VnExpress"))])
    eid = db.create_event(conn, "Tin", "thoi_su_kinh_te")
    db.attach(conn, 1, eid)
    conn.execute("UPDATE events SET score=8.0, source_count=1 WHERE id=?", (eid,))
    conn.commit()
    text = make_article(170).replace("Đây là câu phân tích mẫu", "**Vàng.** Giá giảm [2]. Đây là câu phân tích mẫu", 1)
    db.save_article(conn, eid, parse_article(text, settings), 1)
    page = tmp_path / "p.html"
    page.write_text("<!doctype html><html><head><meta charset=utf8></head><body>" + render(build_data(conn, settings))
                    + "</body></html>", encoding="utf-8")
    out = subprocess.run(["node", "-e", CHECK, str(page)], capture_output=True, text=True, timeout=120)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["errs"] == [] and not r["objectText"] and not r["stars"]
    assert r["strong"] == ["Vàng."] and r["cites"] > 6
    assert r["headings"][0] == "Chuyện gì đã xảy ra" and r["headings"][-1] == "Nguồn"
