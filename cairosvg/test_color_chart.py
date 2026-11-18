"""
Tests for the color comparison chart demonstration.

The demo is both documentation and a regression check: these tests make sure
the chart keeps measuring real CairoSVG output and that every documented
expectation still holds.

"""

import importlib.util
from pathlib import Path

import pytest

DEMO_PATH = Path(__file__).parent.parent / 'demo' / 'color_comparison.py'


@pytest.fixture(scope='module')
def demo():
    """Import the demo script as a module."""
    spec = importlib.util.spec_from_file_location(
        'color_comparison_demo', DEMO_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_cases_match_expected_pixels(demo):
    """Every swatch in the chart renders its documented pixel."""
    cases = demo.build_cases()
    assert cases
    for group in cases:
        for case in group['cases']:
            assert case['matches_expected'], (
                group['title_en'], case['label_en'],
                case['expected'], case['pixels'])


def test_demo_equivalent_spellings_are_identical(demo):
    """Equivalent spellings within a group render identical pixels."""
    cases = demo.build_cases()
    for check in demo.check_equivalence(cases):
        assert check['matches_expected'], check['title_en']
        assert check['equivalent'] is not False, check['title_en']


def test_demo_repeated_export_is_identical(demo):
    """Repeated export of the same document is byte- and pixel-stable."""
    check = demo.check_repeated_export()
    assert check['identical_png']
    assert check['identical_pixels']


def test_demo_writes_a_large_chart(demo, tmp_path):
    """The generated chart is regenerable and larger than 240x160."""
    assert demo.main(['--output-dir', str(tmp_path)]) == 0
    chart = tmp_path / 'color_comparison.png'
    assert chart.is_file()
    from PIL import Image
    with Image.open(chart) as image:
        assert image.width > 240
        assert image.height > 160
    assert (tmp_path / 'color_comparison.json').is_file()
    assert list((tmp_path / 'samples').glob('*.svg'))
