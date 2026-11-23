"""Function selection remains responsive when independent validation takes time."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_latest_function_selection_is_rendered_after_a_slow_previous_request():
    node = shutil.which('node')
    if node is None:
        pytest.skip('needs node')
    subprocess.run([node, str(Path(__file__).with_name('gui_function_selection.js'))],
                   check=True, timeout=30)


def test_function_loading_has_progress_and_a_persistent_error():
    html = (Path(__file__).parents[1] / 'src/decompnet/app/static/index.html').read_text()
    assert 'id="fnLoad"' in html and 'aria-label="Loading function"' in html
    assert 'id="fnLoadError" class="err-msg" role="alert"' in html
