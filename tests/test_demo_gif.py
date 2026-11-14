import http.client
import importlib.util
from pathlib import Path

DEMO_PATH = Path(__file__).resolve().parent.parent / "docs" / "demo.py"


def test_demo_min_gif_starts_from_a_clean_state():
    spec = importlib.util.spec_from_file_location("demo", DEMO_PATH)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    pages = []

    def fetch_page(url, size, png, profile):
        c = http.client.HTTPConnection(url.split("/")[2], timeout=5)
        c.request("GET", "/")
        pages.append(c.getresponse().read().decode())

    demo.shoot_frame = fetch_page
    demo.join_frames = lambda frames, gif: None
    demo.main()
    first_mini = pages[demo.FRAMES]
    assert "~/shop-api | main | ctx 20% | 1 tool calls | 6k in" in first_mini, \
        "demo-min.gif carries over demo.gif's transcripts, token totals or turn start"
