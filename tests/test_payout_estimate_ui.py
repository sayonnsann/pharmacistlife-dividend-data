"""Run the actual checker renderers in JS; inspect generated SVG and notes."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).resolve().parents[1]
JSC = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc")


def test_both_renderers_share_estimates_basis_transition_cannot_overwrite_and_sector_is_masked(tmp_path):
    runtime = shutil.which("node") or (str(JSC) if JSC.exists() else None)
    if not runtime:
        pytest.skip("Node or JavaScriptCore required")
    script = re.search(r"<script>(.*?)</script>", (ROOT / "serving/checker.html").read_text(), re.S).group(1)
    driver = r'''
    var p={code:"7466",name:"fixture",industry:"fixture",
      annual:{2013:13.75,2014:14,2015:15,2017:16},
      payoutRatio:{2015:29,2017:31}, payoutRatioTotalBased:{2015:29,2017:31},
      payoutRatioEstimated:{2013:33.37,2014:30},
      payoutRatioDisplay:{2013:33.37,2014:30,2015:29,2017:31},
      payoutRatioSource:{2013:"dps_eps_estimate",2014:"dps_eps_estimate",2015:"total_cash_paid",2017:"total_cash_paid"},
      basisTransition:{fiscalYear:2015},basisTransitionMetrics:{payoutRatioTotalBased:{2013:999,2015:777}},
      dividendSeries:{basis:"fiscal"}};
    state.financialByCode[p.code]=p;
    state.sectorStats.fixture={payoutRatioTotalBased:{2013:{median:987},2014:{median:987},2015:{median:28},2017:{median:28}}};
    var financial=buildFinancialSection(p.code),captured=[];
    var originalChart=chart;
    chart=function(series,unit,type,label){if(label==="配当性向の推移")captured=series;return originalChart(series,unit,type,label)};
    var metric=miniMetric("配当性向",p.payoutRatioDisplay,"%","line","#2563eb","",null,p,"payoutRatioTotalBased");
    chart=originalChart;
    var bar=buildBarChart(p.annual,p.payoutRatioDisplay,{}, {}, {}, {},p.dividendSeries,p.payoutRatioSource);
    delete p.payoutRatioDisplay;delete p.payoutRatioEstimated;delete p.payoutRatioSource;delete p.basisTransition;delete p.basisTransitionMetrics;
    var legacy=buildFinancialSection(p.code);
    var result=JSON.stringify({financial:financial,metric:metric,bar:bar,series:captured,legacy:legacy});
    if(typeof print==="function")print(result);else console.log(result);
    '''
    script = script.replace("  init()", driver)
    path = tmp_path / "checker-test.js"
    path.write_text("var window={frameElement:null};\n" + script)
    result = subprocess.run([runtime, str(path)], capture_output=True, text=True, check=True)
    rendered = json.loads(result.stdout)
    assert rendered["series"][0]["data"] == {"2013": 33.37, "2014": 30, "2015": 29, "2017": 31}
    assert rendered["series"][1]["data"] == {"2015": 28, "2017": 28}
    for key in ("financial", "metric", "bar"):
        html = rendered[key]
        assert "2013〜2014年は1株配当÷1株利益による推計" in html
        assert "赤字の年は表示しない" in html
        text = re.sub(r"<[^>]*>", "", html)
        assert "999" not in text and "777" not in text and "987" not in text
        svg = ET.fromstring(re.search(r"<svg\b[^>]*>.*?</svg>", html[html.index("配当性向"):] if key == "financial" else html, re.S).group())
        dots = [c for c in svg.iter("circle") if c.find("title") is not None]
        assert len(dots) == 4
        assert ["推計" in c.find("title").text for c in dots] == [True, True, False, False]
        assert [c.get("fill") == "#fff" for c in dots] == [True, True, False, False]
        lines = [c for c in svg.iter("polyline") if c.get("stroke") != "#7c3aed"]
        assert len(lines) == 2  # 2015 to 2017 must not span the missing year
        assert all(c.get("stroke-dasharray") == "5 4" for c in lines)
    assert "横軸は決算期末の年です" in rendered["financial"]
    assert "推計" not in rendered["legacy"]
