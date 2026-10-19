from unittest.mock import MagicMock

import list_sync.database as db
import list_sync.main as main


def test_dismissed_item_survives_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "t.db"))
    db.init_database()
    db.save_sync_result("Dr. Horrible", "movie", None, None, "needs_review", 2008, "14301", "letterboxd", "l")
    item_id = db.sqlite3.connect(db.DB_FILE).execute("SELECT id FROM synced_items").fetchone()[0]

    assert db.is_dismissed("14301", None) is False
    assert db.set_review_status(item_id, "dismissed") is True
    assert db.set_review_status(item_id, "dismissed") is False
    assert db.is_dismissed(14301, None) is True

    client = MagicMock()
    item = {"title": "Dr. Horrible", "media_type": "movie", "year": 2008, "tmdb_id": 14301}
    assert main.process_media_item(item, client, dry_run=False)["status"] == "skipped"
    client.get_media_by_tmdb_id.assert_not_called()
    assert db.is_dismissed("14301", None) is True

    assert db.set_review_status(item_id, "needs_review") is True
    assert db.is_dismissed("14301", None) is False
