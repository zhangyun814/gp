import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.analyzer import candidate_keywords, extract_topic, return_metrics
from app.db import Base
from app.models import PlanetTopic, Stock, TopicStock


class StockRecognitionTest(unittest.TestCase):
    def test_stock_name_from_master_is_recognized(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add(Stock(stock_code="300499", stock_name="高澜股份", exchange="SZ"))
            topic = PlanetTopic(topic_id="highlan", title="高澜股份董事长交流要点", content="海外液冷订单增长",
                                author="Geek", published_at=datetime.now(timezone.utc), source_url="")
            db.add(topic)
            db.flush()
            extract_topic(db, topic)
            db.flush()
            link = db.scalar(select(TopicStock).where(TopicStock.topic_id == topic.id))
            self.assertIsNotNone(link)
            self.assertEqual(db.get(Stock, link.stock_id).stock_code, "300499")
        finally:
            db.close()
            engine.dispose()

    def test_one_month_threshold_requires_twenty_trading_days(self):
        class Quote:
            def __init__(self, close):
                self.close = close

        self.assertIsNone(return_metrics(10, [Quote(11)] * 19)["max_return_20d"])
        self.assertEqual(return_metrics(Decimal("10"), [Quote(11)] * 20)["max_return_20d"], Decimal("0.1"))

    def test_auto_keywords_extract_emphasis_phrases_not_industry_nouns(self):
        terms = candidate_keywords(
            "深信服继续看好，业绩超预期，翻10倍空间，强 CALL，务必重视，重点推荐，液冷订单",
            {"深信服"},
        )
        self.assertTrue({"继续看好", "超预期", "翻10倍空间", "强call", "务必重视", "重点推荐"} <= terms)
        self.assertNotIn("深信服", terms)
        self.assertNotIn("液冷", terms)
        self.assertNotIn("订单", terms)
        self.assertNotIn("0倍空间", terms)


if __name__ == "__main__":
    unittest.main()
