"""Живой контракт пагинации разговоров: курсор, поиск, отсутствие пропусков и дублей.

Запуск (стенд :8100):
    HUB_TOKEN=... python -m pytest -q tests/test_conversation_pagination.py
Без HUB_TOKEN тест пропускается, чтобы не ломать модульный прогон.
"""
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8100")
TOKEN = os.environ.get("HUB_TOKEN", "")


def call(path, method="GET", body=None, expected=200):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        status = error.code
        payload = json.loads(error.read().decode("utf-8") or "{}")
    assert status == expected, "expected %s, got %s: %s" % (expected, status, payload)
    return payload


@unittest.skipUnless(TOKEN, "нужен HUB_TOKEN стенда")
class ConversationPaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        projects = call("/projects")["projects"]
        found = [item for item in projects if item["name"] == "UX-03 пагинация"]
        cls.project = found[0] if found else call("/projects", "POST",
                                                  {"name": "UX-03 пагинация", "host_id": "server"})
        cls.created = []
        for index in range(30):
            title = "Страница %02d: разговор для проверки курсора" % index
            cls.created.append(call("/projects/" + cls.project["id"] + "/conversations", "POST",
                                    {"role_id": "architect", "title": title}))

    def page(self, **params):
        query = urllib.parse.urlencode(params)
        return call("/projects/" + self.project["id"] + "/conversations?" + query)

    def test_pages_cover_whole_list_without_gaps_or_duplicates(self):
        seen, cursor, pages = [], "", 0
        while True:
            page = self.page(limit=7, cursor=cursor)
            pages += 1
            seen.extend(item["id"] for item in page["conversations"])
            self.assertEqual(page["total"], len(call("/projects/" + self.project["id"] + "/conversations")["conversations"]))
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            self.assertTrue(cursor, "has_more требует курсор")
            self.assertLess(pages, 40)
        self.assertEqual(len(seen), len(set(seen)), "страницы не должны повторять строки")
        expected = {item["id"] for item in self.created}
        self.assertTrue(expected.issubset(set(seen)), "все созданные разговоры должны быть на страницах")
        self.assertGreaterEqual(pages, 4)

    def test_new_conversation_during_paging_does_not_shift_rows(self):
        first = self.page(limit=5)
        first_ids = [item["id"] for item in first["conversations"]]
        extra = call("/projects/" + self.project["id"] + "/conversations", "POST",
                     {"role_id": "architect", "title": "Появился во время листания"})
        second = self.page(limit=100, cursor=first["next_cursor"])
        second_ids = [item["id"] for item in second["conversations"]]
        self.assertFalse(set(first_ids) & set(second_ids), "следующая страница не повторяет предыдущую")
        self.assertNotIn(extra["id"], second_ids, "новая запись выше курсора не попадает на следующую страницу")
        self.assertEqual(len(second_ids), len(set(second_ids)))
        older = [item["id"] for item in self.created if item["id"] not in first_ids]
        self.assertTrue(set(older).issubset(set(second_ids)), "старые строки не теряются из-за новой записи")

    def test_search_filters_and_reports_total(self):
        page = self.page(limit=100, q="Страница 1")
        titles = [item["title"] for item in page["conversations"]]
        self.assertTrue(titles, "поиск должен что-то найти")
        self.assertTrue(all("Страница 1" in title for title in titles))
        self.assertEqual(page["total"], len(page["conversations"]))
        empty = self.page(limit=10, q="совпадений-точно-нет-12345")
        self.assertEqual(empty["conversations"], [])
        self.assertEqual(empty["total"], 0)

    def test_invalid_cursor_is_rejected(self):
        call("/projects/" + self.project["id"] + "/conversations?limit=5&cursor=%%%", expected=400)

    def test_default_call_still_returns_full_list(self):
        full = call("/projects/" + self.project["id"] + "/conversations")
        self.assertIn("conversations", full)
        self.assertGreaterEqual(len(full["conversations"]), len(self.created))


if __name__ == "__main__":
    unittest.main()
