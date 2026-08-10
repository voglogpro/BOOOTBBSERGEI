from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


DEFAULT_RULESET_VERSION = "ru-franchise-intent-v2"
REVIEW_THRESHOLD = 35
LEAD_THRESHOLD = 55


@dataclass(frozen=True, slots=True)
class WeightedRule:
    rule_id: str
    label: str
    points: int
    pattern: str


@dataclass(frozen=True, slots=True)
class RuleMatch:
    rule_id: str
    label: str
    points: int
    fragment: str


@dataclass(frozen=True, slots=True)
class IntentResult:
    score: int
    decision: str
    rules_version: str
    matches: tuple[RuleMatch, ...]

    @property
    def matched_rules_json(self) -> str:
        return json.dumps(
            [asdict(match) for match in self.matches],
            ensure_ascii=False,
            separators=(",", ":"),
        )


RULES: tuple[WeightedRule, ...] = (
    WeightedRule(
        "seek_franchise",
        "Человек ищет или рассматривает франшизу",
        60,
        r"\b(?:ищу|ищем|подбираю|подбираем|рассматриваю|рассматриваем|хочу|хотим|интересует|посоветуйте|порекомендуйте)\b.{0,70}\bфраншиз\w*",
    ),
    WeightedRule(
        "which_franchise",
        "Запрос рекомендации франшизы",
        60,
        r"\b(?:какую|какая|какие)\s+франшиз\w*",
    ),
    WeightedRule(
        "invest_where",
        "Ищет направление для вложений",
        55,
        r"\b(?:во\s+что|куда)\s+(?:можно\s+)?(?:вложить|инвестировать|вложиться)\b",
    ),
    WeightedRule(
        "ready_to_invest",
        "Заявляет готовность инвестировать",
        45,
        r"\b(?:готов|готова|готовы|хочу|планирую)\b.{0,35}\b(?:вложить|инвестировать)\b",
    ),
    WeightedRule(
        "open_business",
        "Хочет открыть или запустить бизнес",
        55,
        r"\b(?:(?:какой|какое)\s+бизнес\s+(?:открыть|запустить)|(?:хочу|планирую|думаю)\s+(?:открыть|запустить)\s+(?:свой\s+)?бизнес)\b",
    ),
    WeightedRule(
        "seek_business",
        "Ищет готовый бизнес или проект",
        45,
        r"\b(?:ищу|подбираю|рассматриваю)\b.{0,35}\b(?:готовый\s+бизнес|бизнес|инвестиционн\w*\s+проект)\b",
    ),
    WeightedRule(
        "buy_business",
        "Хочет купить бизнес или долю в действующем бизнесе",
        55,
        r"\b(?:куплю|хочу\s+купить|готов\w*\s+купить|приобрету|рассмотрю\s+покупку)\b.{0,50}\b(?:готов\w*\s+)?бизнес\w*\b",
    ),
    WeightedRule(
        "seek_investment_target",
        "Ищет проект или бизнес для собственных инвестиций",
        50,
        r"\b(?:ищу|подбираю|рассматриваю|рассмотрю)\b.{0,45}\b(?:проект|бизнес)\w*\b.{0,35}\b(?:для\s+инвестиц\w*|для\s+вложен\w*|куда\s+вложить)\b",
    ),
    WeightedRule(
        "capital_available",
        "Готов предоставить собственный капитал",
        55,
        r"\b(?:инвестирую|вложу)\b.{0,45}\b(?:\d+(?:[.,]\d+)?\s*(?:млн|миллион\w*|тыс\w*)|в\s+(?:готовый\s+)?(?:бизнес|проект))\b",
    ),
    WeightedRule(
        "franchise_word",
        "Упомянута франшиза",
        15,
        r"\bфраншиз\w*\b",
    ),
    WeightedRule(
        "budget_amount",
        "Указан инвестиционный бюджет",
        18,
        r"\b(?:бюджет|капитал|есть|вложить|инвестировать)\b.{0,30}\b\d+(?:[.,]\d+)?\s*(?:млн|миллион\w*|тыс\w*)\b",
    ),
    WeightedRule(
        "seller_franchise",
        "Продавец рекламирует свою франшизу",
        -75,
        r"\b(?:продаю|продам|продаем|продается|предлагаю|предлагаем|упакуем|запустили)\b.{0,45}\bфраншиз\w*",
    ),
    WeightedRule(
        "seeking_investor",
        "Автору нужен инвестор, а не объект для вложений",
        -85,
        r"\b(?:ищу|ищем|нужен|нужны|требуется|привлекаю|привлекаем)\b.{0,45}\bинвестор\w*\b",
    ),
    WeightedRule(
        "raising_financing",
        "Автор привлекает финансирование в свой проект",
        -85,
        r"\b(?:ищу|ищем|нужно|требуется|привлекаю|привлекаем)\b.{0,45}\b(?:инвестиц\w*|финансирован\w*|капитал\w*)\b",
    ),
    WeightedRule(
        "selling_business",
        "Автор продаёт бизнес, проект или долю",
        -75,
        r"\b(?:продаю|продам|продаем|продается|предлагаю\s+к\s+покупке)\b.{0,45}\b(?:готов\w*\s+)?(?:бизнес|проект|дол[юяеи])\b",
    ),
    WeightedRule(
        "crypto_or_mlm",
        "Криптовалюта, финансовая пирамида или MLM",
        -75,
        r"\b(?:крипт\w*|ico|токен\w*|forex|форекс|mlm|сетев\w*\s+маркетинг|финансов\w*\s+пирамид\w*)\b",
    ),
    WeightedRule(
        "infobusiness",
        "Инфобизнес или обучение",
        -60,
        r"\b(?:курс\w*|обучени\w*|вебинар\w*|наставнич\w*|интенсив\w*|марафон\w*|мастер[ -]?класс\w*)\b",
    ),
    WeightedRule(
        "promotional_post",
        "Рекламный призыв",
        -55,
        r"\b(?:оставьте\s+заявку|успейте\s+записаться|мест\s+осталось|регистрация\s+по\s+ссылке|пишите\s+в\s+личку)\b",
    ),
    WeightedRule(
        "job_post",
        "Вакансия или поиск работы",
        -40,
        r"\b(?:ваканси\w*|ищу\s+работу|резюме|требуется\s+сотрудник)\b",
    ),
)


def normalize_text(text: str) -> str:
    normalized = text.lower().replace("ё", "е")
    normalized = re.sub(r"[\u00a0\s]+", " ", normalized)
    return normalized.strip()


def classify_intent(text: str) -> IntentResult:
    normalized = normalize_text(text)
    matches: list[RuleMatch] = []
    positive_points = 0
    total = 0

    for rule in RULES:
        match = re.search(rule.pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        fragment = match.group(0).strip()[:160]
        matches.append(RuleMatch(rule.rule_id, rule.label, rule.points, fragment))
        total += rule.points
        if rule.points > 0:
            positive_points += rule.points

    score = max(0, min(100, total))
    if positive_points == 0 or score < REVIEW_THRESHOLD:
        decision = "rejected"
    elif score < LEAD_THRESHOLD:
        decision = "review"
    else:
        decision = "lead"

    return IntentResult(
        score=score,
        decision=decision,
        rules_version=DEFAULT_RULESET_VERSION,
        matches=tuple(matches),
    )


def ruleset_json() -> str:
    payload = {
        "version": DEFAULT_RULESET_VERSION,
        "review_threshold": REVIEW_THRESHOLD,
        "lead_threshold": LEAD_THRESHOLD,
        "rules": [asdict(rule) for rule in RULES],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
