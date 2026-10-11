"""
Telegram-бот с двумя режимами расчёта:
1. course_grade — сколько баллов нужно набрать на финале курса, чтобы
   получить желаемую итоговую оценку (с учётом нескольких составных
   оценок: assignments, quizzes, midterm и т.д.).
2. cgpa — как изменится кумулятивный CGPA после семестра, либо какой
   GPA нужен за семестр, чтобы получить желаемый CGPA.

Установка зависимостей:
    pip install pyTelegramBotAPI google-genai pydantic

Токены берутся из переменных окружения:
    export TELEGRAM_BOT_TOKEN="..."
    export GEMINI_API_KEY="..."

Запуск:
    python bot.py
"""

import os
from typing import Literal, Optional

import telebot
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
gemini = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = (
    "Ты извлекаешь из сообщения студента данные для одного из двух "
    "расчётов и определяешь request_type:\n\n"
    "1. \"course_grade\" — вопрос про оценку ВНУТРИ ОДНОГО КУРСА: сколько "
    "нужно набрать на финале, зная баллы за assignments/quizzes/midterm и "
    "их веса в процентах ОТ ИТОГА ЭТОГО КУРСА. Заполни поле course:\n"
    "  - components: список работ, для каждой:\n"
    "    * name — короткое название, если есть в тексте (иначе null)\n"
    "    * score — набранные баллы\n"
    "    * max_score — максимум баллов. Если не указан явно, используй 100 "
    "— кроме случаев, когда score дан как доля от 1 (например, 0.85), "
    "тогда max_score = 1\n"
    "    * weight_percent — вес этой работы в процентах от итоговой оценки "
    "курса\n"
    "  - target_gpa_or_score — желаемая оценка ТОЧНО как написана в "
    "тексте, строкой: число-процент (\"85\"), число GPA (\"4.0\", \"3.7\") "
    "или буква (\"A\", \"B+\")\n"
    "  - final_weight_percent — вес финала в процентах, ЕСЛИ указан явно, "
    "иначе null\n\n"
    "2. \"cgpa\" — вопрос про КУМУЛЯТИВНЫЙ CGPA ЗА НЕСКОЛЬКО СЕМЕСТРОВ: "
    "как изменится общий CGPA после семестра, или какой GPA нужен за "
    "семестр для желаемого CGPA. Заполни поле cgpa:\n"
    "  - current_cgpa — нынешний CGPA студента (число по шкале до 4.0)\n"
    "  - completed_credits — сколько кредитов уже закрыто/пройдено всего\n"
    "  - semester_credits — сколько кредитов весит семестр, о котором речь\n"
    "  - semester_gpa — GPA, полученный или гипотетический за этот "
    "семестр, ЕСЛИ он есть в тексте (тогда считаем новый CGPA), иначе null\n"
    "  - target_cgpa — желаемый итоговый CGPA после этого семестра, ЕСЛИ "
    "он есть в тексте (тогда считаем нужный GPA за семестр), иначе null\n"
    "  - total_semesters — общее число семестров за весь период обучения, "
    "ЕСЛИ упомянуто в тексте, иначе null\n"
    "  - completed_semesters — сколько семестров уже пройдено, ЕСЛИ "
    "упомянуто в тексте, иначе null\n\n"
    "Заполняй только соответствующий request_type блок (course или cgpa), "
    "второй оставляй null. Извлекай только то, что явно следует из текста. "
    "Не выдумывай числа."
)


class Component(BaseModel):
    name: Optional[str] = Field(default=None, description="Название работы, если указано")
    score: float = Field(description="Набранные баллы за эту работу")
    max_score: float = Field(default=100.0, description="Максимум баллов за эту работу")
    weight_percent: float = Field(
        description="Вес этой работы в процентах от итоговой оценки"
    )


class GradeCalculationInput(BaseModel):
    components: list[Component] = Field(description="Все пройденные работы")
    target_gpa_or_score: str = Field(
        description=(
            "Желаемая оценка как она написана в тексте: число-процент, "
            "число GPA или буквенная оценка"
        )
    )
    final_weight_percent: Optional[float] = Field(
        default=None,
        description="Вес финала в процентах, если указан явно в тексте, иначе null",
    )


class CgpaCalculationInput(BaseModel):
    current_cgpa: float = Field(description="Нынешний CGPA (шкала до 4.0)")
    completed_credits: float = Field(description="Сколько кредитов уже закрыто всего")
    semester_credits: float = Field(description="Сколько кредитов весит рассматриваемый семестр")
    semester_gpa: Optional[float] = Field(
        default=None, description="GPA за этот семестр, если известен, иначе null"
    )
    target_cgpa: Optional[float] = Field(
        default=None, description="Желаемый итоговый CGPA после семестра, иначе null"
    )
    total_semesters: Optional[int] = Field(
        default=None, description="Всего семестров за период обучения, если упомянуто"
    )
    completed_semesters: Optional[float] = Field(
        default=None, description="Сколько семестров уже пройдено, если упомянуто (может быть дробным)"
    )


class BotRequest(BaseModel):
    request_type: Literal["course_grade", "cgpa"] = Field(
        description="Тип запроса: оценка внутри курса или кумулятивный CGPA"
    )
    course: Optional[GradeCalculationInput] = Field(
        default=None, description="Заполняется, если request_type == course_grade"
    )
    cgpa: Optional[CgpaCalculationInput] = Field(
        default=None, description="Заполняется, если request_type == cgpa"
    )


# Стандартная шкала: буквенная оценка -> GPA -> процент (для режима course_grade).
LETTER_TO_GPA = {
    "A": 4.0,
    "A-": 3.7,
    "B+": 3.3,
    "B": 3.0,
    "B-": 2.7,
    "C+": 2.3,
    "C": 2.0,
}
GPA_TO_PERCENT = {
    4.0: 85.0,
    3.7: 80.0,
    3.3: 75.0,
    3.0: 70.0,
    2.7: 65.0,
    2.3: 60.0,
    2.0: 55.0,
}


def gpa_to_percent(gpa: float) -> float:
    closest = min(GPA_TO_PERCENT, key=lambda g: abs(g - gpa))
    if abs(closest - gpa) > 0.05:
        supported = ", ".join(str(g) for g in sorted(GPA_TO_PERCENT, reverse=True))
        raise ValueError(f"не знаю, как перевести GPA {gpa} в процент (поддерживаю: {supported})")
    return GPA_TO_PERCENT[closest]


def parse_target_percent(raw: str) -> float:
    """Понимает сырой процент ("85"), GPA ("4.0") или букву ("A", "B+")."""
    normalized = raw.strip()
    letter_key = normalized.upper()
    if letter_key in LETTER_TO_GPA:
        return gpa_to_percent(LETTER_TO_GPA[letter_key])

    try:
        value = float(normalized.replace(",", "."))
    except ValueError:
        raise ValueError(f"не получилось распознать цель «{raw}»")

    # Числа в диапазоне GPA (макс. 4.0) считаем шкалой GPA, а не процентом.
    if value <= 4.3:
        return gpa_to_percent(value)
    return value


def extract_bot_request(text: str) -> BotRequest:
    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=BotRequest,
        ),
    )
    return response.parsed


def calculate_needed_final_score(data: GradeCalculationInput) -> dict:
    current_total_weighted_percent = sum(
        c.score / c.max_score * c.weight_percent for c in data.components
    )
    current_total_weight = sum(c.weight_percent for c in data.components)

    final_weight = data.final_weight_percent
    if final_weight is None:
        final_weight = 100 - current_total_weight
    if final_weight <= 0:
        raise ValueError("вес финала должен быть больше 0%")

    target_percent = parse_target_percent(data.target_gpa_or_score)
    needed = ((target_percent - current_total_weighted_percent) / final_weight) * 100

    return {
        "current_total_weighted_percent": current_total_weighted_percent,
        "current_total_weight": current_total_weight,
        "final_weight": final_weight,
        "target_percent": target_percent,
        "needed": needed,
    }


def calculate_cgpa(data: CgpaCalculationInput) -> dict:
    if data.semester_credits <= 0:
        raise ValueError("вес семестра в кредитах должен быть больше 0")

    new_total_credits = data.completed_credits + data.semester_credits

    if data.semester_gpa is not None:
        mode = "new_cgpa"
        new_cgpa = (
            data.current_cgpa * data.completed_credits
            + data.semester_gpa * data.semester_credits
        ) / new_total_credits
        needed_gpa = None
    elif data.target_cgpa is not None:
        mode = "needed_gpa"
        needed_gpa = (
            data.target_cgpa * new_total_credits
            - data.current_cgpa * data.completed_credits
        ) / data.semester_credits
        new_cgpa = None
    else:
        raise ValueError(
            "укажи либо ожидаемый/полученный GPA за семестр, либо желаемый итоговый CGPA"
        )

    result = {
        "mode": mode,
        "new_cgpa": new_cgpa,
        "needed_gpa": needed_gpa,
        "new_total_credits": new_total_credits,
    }

    # Дополнительный контекст про всю программу обучения — только если
    # студент упомянул общее число семестров (приблизительная оценка при
    # допущении равномерной кредитной нагрузки по семестрам).
    if data.total_semesters:
        if data.completed_semesters:
            avg_credits_per_semester = data.completed_credits / data.completed_semesters
        else:
            avg_credits_per_semester = data.semester_credits
        total_program_credits = avg_credits_per_semester * data.total_semesters
        semester_weight_percent = data.semester_credits / total_program_credits * 100
        remaining_semesters_after = data.total_semesters - (data.completed_semesters or 0) - 1

        result["total_program_credits"] = total_program_credits
        result["semester_weight_percent"] = semester_weight_percent
        result["remaining_semesters_after"] = remaining_semesters_after

    return result


@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message) -> None:
    bot.reply_to(
        message,
        "Привет! Я умею считать два типа вопросов про учёбу.\n\n"
        "1) Оценка внутри курса — опиши составные оценки и цель:\n"
        "«По курсу у меня: assignment 1 — 18/20 (вес 10%), quiz — 8.5/10 "
        "(вес 5%), midterm — 78/100 (вес 25%). Хочу получить A по курсу.»\n\n"
        "2) Кумулятивный CGPA — опиши нынешний CGPA, закрытые кредиты и "
        "семестр:\n"
        "«Нынешний CGPA 3.64, закрыл 90 кредитов, следующий семестр весит "
        "20 кредитов, сколько нужно набрать чтобы CGPA стал 3.8?» — или "
        "«если наберу 3.91 за этот семестр, каким станет новый CGPA?»\n\n"
        "Можно также упомянуть общее число семестров за период обучения — "
        "покажу, сколько примерно весит этот семестр от всей программы.",
    )


@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message) -> None:
    bot.send_chat_action(message.chat.id, "typing")

    try:
        parsed = extract_bot_request(message.text)
    except genai_errors.APIError as e:
        bot.reply_to(message, f"Ошибка при обращении к Gemini API: {e}")
        return
    except Exception:
        bot.reply_to(
            message,
            "Не получилось разобрать сообщение. Убедись, что в тексте есть "
            "все нужные числа (оценки/баллы, веса, кредиты). Попробуй "
            "переформулировать.",
        )
        return

    if parsed.request_type == "course_grade" and parsed.course:
        handle_course_grade(message, parsed.course)
    elif parsed.request_type == "cgpa" and parsed.cgpa:
        handle_cgpa(message, parsed.cgpa)
    else:
        bot.reply_to(
            message,
            "Не получилось понять, какой это расчёт. Опиши либо оценки "
            "внутри курса и цель, либо нынешний CGPA/кредиты/семестр.",
        )


def handle_course_grade(message: telebot.types.Message, data: GradeCalculationInput) -> None:
    try:
        result = calculate_needed_final_score(data)
    except ValueError as e:
        bot.reply_to(message, f"Не получилось посчитать: {e}.")
        return

    breakdown_lines = []
    for c in data.components:
        label = c.name or "без названия"
        contribution = c.score / c.max_score * c.weight_percent
        breakdown_lines.append(
            f"• {label}: {c.score}/{c.max_score} (вес {c.weight_percent}%) → {contribution:.1f}%"
        )

    summary = (
        "\n".join(breakdown_lines)
        + f"\n\nНабрано: {result['current_total_weighted_percent']:.1f}% из "
        f"{result['current_total_weight']:.1f}% возможных\n"
        f"Вес финала: {result['final_weight']:.1f}%\n"
        f"Нужный итоговый процент: {result['target_percent']:.1f}%\n\n"
    )

    needed_score = result["needed"]
    if needed_score > 100:
        reply = (
            summary
            + f"Нужно набрать {needed_score:.1f} балла (из 100) — это больше "
            "максимума, так что при таком весе цель, к сожалению, "
            "недостижима."
        )
    elif needed_score <= 0:
        reply = (
            summary
            + "Цель уже достигнута — на финале достаточно набрать 0 баллов "
            "(или даже меньше, но это не считается)."
        )
    else:
        reply = summary + f"Нужно набрать примерно {needed_score:.1f} балла (из 100) на финале."

    bot.reply_to(message, reply)


def handle_cgpa(message: telebot.types.Message, data: CgpaCalculationInput) -> None:
    try:
        result = calculate_cgpa(data)
    except ValueError as e:
        bot.reply_to(message, f"Не получилось посчитать: {e}.")
        return

    summary = (
        f"Нынешний CGPA: {data.current_cgpa} (закрыто {data.completed_credits:.0f} кредитов)\n"
        f"Семестр весит: {data.semester_credits:.0f} кредитов\n"
        f"После семестра всего кредитов: {result['new_total_credits']:.0f}\n\n"
    )

    if result["mode"] == "new_cgpa":
        reply = summary + f"Если получишь GPA {data.semester_gpa} за семестр, новый CGPA станет ≈ {result['new_cgpa']:.3f}."
    else:
        needed = result["needed_gpa"]
        if needed > 4.0:
            reply = (
                summary
                + f"Нужен GPA {needed:.2f} за семестр — это больше максимума (4.0), "
                "так что при такой кредитной нагрузке цель, к сожалению, "
                "недостижима за один семестр."
            )
        elif needed <= 0:
            reply = (
                summary
                + "Цель уже достигнута — за семестр достаточно не завалить курсы совсем "
                "(GPA 0 и то хватит по формуле, но так на деле не бывает)."
            )
        else:
            reply = summary + f"Нужно набрать примерно {needed:.2f} GPA за семестр, чтобы CGPA стал {data.target_cgpa}."

    if "total_program_credits" in result:
        reply += (
            f"\n\nПри допущении равномерной нагрузки по семестрам: программа "
            f"~{result['total_program_credits']:.0f} кредитов, этот семестр "
            f"весит ~{result['semester_weight_percent']:.1f}% от всей учёбы, "
            f"после него останется ~{result['remaining_semesters_after']:.1f} семестр(ов)."
        )

    bot.reply_to(message, reply)


if __name__ == "__main__":
    print("Бот запущен...")
    bot.infinity_polling()
