import os
import sys
import json
import pytest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.anti_spam import AntiSpamProcessor
from core.session_manager import FacebookSessionManager
from main import NightSchedulerWorker


def test_anti_spam_spintax():
    text = "{Xin chào|Hello} {thế giới|mọi người}!"
    results = set()
    for _ in range(20):
        spun = AntiSpamProcessor.spin_text(text)
        results.add(spun)
    assert len(results) > 1
    assert any("Xin chào" in r or "Hello" in r for r in results)


def test_anti_spam_image_mutation(tmp_path):
    img_path = tmp_path / "test.jpg"
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    img.save(img_path, "JPEG")

    out_dir = tmp_path / "mutated"
    mutated_file = AntiSpamProcessor.mutate_image(str(img_path), str(out_dir))

    assert os.path.exists(mutated_file)
    assert mutated_file != str(img_path)

    # Check size
    mut_img = Image.open(mutated_file)
    assert mut_img.size[0] <= 100
    assert mut_img.size[1] <= 100


def test_session_manager_cookie_parsing():
    mgr = FacebookSessionManager(profile_dir="./test_profile")
    cookie_str = "c_user=100012345; xs=abc987; datr=xyz123; i_user=9999"
    cookies = []
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            name, value = item.split("=", 1)
            if name.strip() == "i_user":
                continue
            cookies.append({"name": name.strip(), "value": value.strip()})

    assert len(cookies) == 3
    names = [c["name"] for c in cookies]
    assert "i_user" not in names
    assert "c_user" in names
    assert "xs" in names
    assert "datr" in names


def test_night_scheduler_worker_data(tmp_path):
    sample_data = {
        "account_cookie": "test_cookie",
        "tasks": [
            {"page_id": "123", "content": "Test post", "status": "pending"}
        ]
    }
    data_file = tmp_path / "pages.json"
    data_file.write_text(json.dumps(sample_data), encoding="utf-8")

    worker = NightSchedulerWorker(data_file=str(data_file), profile_dir=str(tmp_path / "prof"))
    data = worker.load_data()
    assert data["account_cookie"] == "test_cookie"
    assert len(data["tasks"]) == 1
