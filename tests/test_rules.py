from __future__ import annotations

import unittest

from app.rules import classify_intent


class IntentRulesTests(unittest.TestCase):
    def test_direct_franchise_request_is_a_lead(self) -> None:
        result = classify_intent(
            "Ищу франшизу для Екатеринбурга, готов вложить 7 млн рублей"
        )
        self.assertEqual(result.decision, "lead")
        self.assertGreaterEqual(result.score, 55)
        self.assertIn("seek_franchise", {match.rule_id for match in result.matches})

    def test_investment_question_with_budget_is_a_lead(self) -> None:
        result = classify_intent("Подскажите, во что вложить бюджет 5 млн?")
        self.assertEqual(result.decision, "lead")
        self.assertGreaterEqual(result.score, 55)

    def test_business_request_is_a_lead(self) -> None:
        result = classify_intent("Рассматриваю готовый бизнес, бюджет 6 млн")
        self.assertEqual(result.decision, "lead")

    def test_infobusiness_ad_is_rejected(self) -> None:
        result = classify_intent(
            "Предлагаем франшизу онлайн-курса. Оставьте заявку на вебинар"
        )
        self.assertEqual(result.decision, "rejected")
        self.assertEqual(result.score, 0)

    def test_franchise_word_without_purchase_intent_is_rejected(self) -> None:
        result = classify_intent("Новости рынка франшиз за эту неделю")
        self.assertEqual(result.decision, "rejected")

    def test_buyer_of_existing_business_is_a_lead(self) -> None:
        result = classify_intent("Куплю готовый бизнес, бюджет до 8 млн рублей")
        self.assertEqual(result.decision, "lead")
        self.assertIn("buy_business", {match.rule_id for match in result.matches})

    def test_person_seeking_investor_is_not_our_lead(self) -> None:
        result = classify_intent(
            "Ищу инвестора в готовый бизнес, нужно 6 млн на расширение"
        )
        self.assertEqual(result.decision, "rejected")
        self.assertIn("seeking_investor", {match.rule_id for match in result.matches})

    def test_business_seller_is_not_our_lead(self) -> None:
        result = classify_intent("Продам готовый бизнес за 5 млн рублей")
        self.assertEqual(result.decision, "rejected")
        self.assertIn("selling_business", {match.rule_id for match in result.matches})

    def test_investor_with_available_capital_is_a_lead(self) -> None:
        result = classify_intent("Инвестирую до 7 млн в готовый бизнес")
        self.assertEqual(result.decision, "lead")
        self.assertIn("capital_available", {match.rule_id for match in result.matches})

    def test_crypto_offer_is_rejected(self) -> None:
        result = classify_intent("Готов вложить 5 млн в криптовалютный проект")
        self.assertEqual(result.decision, "rejected")


if __name__ == "__main__":
    unittest.main()
