import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import search_topics, split_keywords
from app.models import PlanetTopic


class TopicSearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        cls.Session = sessionmaker(bind=engine)

    def setUp(self):
        self.db = self.Session()
        self.db.add_all([
            PlanetTopic(topic_id="older", title="深信服", content="有望翻倍", author="A",
                        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            PlanetTopic(topic_id="newer", title="深信服翻倍", content="继续跟踪", author="B",
                        published_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            PlanetTopic(topic_id="one-word", title="深信服", content="仅一个词", author="C",
                        published_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_split_keywords_accepts_commas_spaces_and_duplicates(self):
        self.assertEqual(split_keywords(" 深信服，翻倍, 深信服 "), ["深信服", "翻倍"])

    def test_search_requires_all_terms_and_orders_by_source_time(self):
        first_page = search_topics("深信服 翻倍", page=1, page_size=1, db=self.db)
        second_page = search_topics("深信服 翻倍", page=2, page_size=1, db=self.db)
        self.assertEqual(first_page["total"], 2)
        self.assertEqual(first_page["items"][0]["topic_id"], "newer")
        self.assertEqual(second_page["items"][0]["topic_id"], "older")
        self.assertEqual(first_page["items"][0]["published_at"], datetime(2026, 1, 2, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
