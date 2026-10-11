"""
Telegram bot with two calculation modes:
1. course_grade - how many points are needed on a course final exam to
   reach the target overall grade (based on several graded components:
   assignments, quizzes, midterm, etc.).
2. cgpa - how the cumulative CGPA changes after a semester, or what
   semester GPA is needed to reach the target CGPA.

Install dependencies:
    pip install pyTelegramBotAPI google-genai pydantic

Tokens are read from environment variables:
    export TELEGRAM_BOT_TOKEN="..."
    export GEMINI_API_KEY="..."

Run:
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
    "You extract data from a student's message for one of two "
    "calculations and set request_type:\n\n"
    "1. \"course_grade\" - a question about a grade WITHIN ONE COURSE: how "
    "much is needed on the final exam, given the scores for "
    "assignments/quizzes/midterm and their weights as a percentage OF THIS "
    "COURSE'S TOTAL. Fill in the course field:\n"
    "  - components: list of graded items, for each one:\n"
    "    * name - a short name if the text gives one (otherwise null)\n"
    "    * score - points earned\n"
    "    * max_score - maximum points. If not stated explicitly, use 100 "
    "- unless score is given as a fraction of 1 (for example, 0.85), "
    "then max_score = 1\n"
    "    * weight_percent - weight of this item as a percentage of the "
    "course's final grade\n"
    "  - target_gpa_or_score - the target grade EXACTLY as written in the "
    "text, as a string: a percentage (\"85\"), a GPA value (\"4.0\", \"3.7\") "
    "or a letter (\"A\", \"B+\")\n"
    "  - final_weight_percent - weight of the final exam in percent, IF "
    "stated explicitly, otherwise null\n\n"
    "2. \"cgpa\" - a question about CUMULATIVE CGPA ACROSS SEVERAL SEMESTERS: "
    "how the overall CGPA changes after a semester, or what semester GPA is "
    "needed for a target CGPA. Fill in the cgpa field:\n"
    "  - current_cgpa - the student's current CGPA (on a scale up to 4.0)\n"
    "  - completed_credits - total credits already completed\n"
    "  - semester_credits - how many credits the semester in question carries\n"
    "  - semester_gpa - the GPA earned or hypothetical for this semester, "
    "IF given in the text (then we calculate the new CGPA), otherwise null\n"
    "  - target_cgpa - the desired CGPA after this semester, IF given in "
    "the text (then we calculate the needed semester GPA), otherwise null\n"
    "  - total_semesters - the total number of semesters in the whole "
    "program, IF mentioned in the text, otherwise null\n"
    "  - completed_semesters - how many semesters are already completed, IF "
    "mentioned in the text, otherwise null\n\n"
    "Fill in only the block matching request_type (course or cgpa) and "
    "leave the other one null. Extract only what clearly follows from the "
    "text. Do not make up numbers."
)


class Component(BaseModel):
    name: Optional[str] = Field(default=None, description="Name of the graded item, if given")
    score: float = Field(description="Points earned for this item")
    max_score: float = Field(default=100.0, description="Maximum points for this item")
    weight_percent: float = Field(
        description="Weight of this item as a percentage of the final grade"
    )


class GradeCalculationInput(BaseModel):
    components: list[Component] = Field(description="All completed graded items")
    target_gpa_or_score: str = Field(
        description=(
            "Target grade as written in the text: a percentage, "
            "a GPA value or a letter grade"
        )
    )
    final_weight_percent: Optional[float] = Field(
        default=None,
        description="Weight of the final exam in percent, if stated explicitly in the text, otherwise null",
    )


class CgpaCalculationInput(BaseModel):
    current_cgpa: float = Field(description="Current CGPA (scale up to 4.0)")
    completed_credits: float = Field(description="Total credits already completed")
    semester_credits: float = Field(description="Credits carried by the semester in question")
    semester_gpa: Optional[float] = Field(
        default=None, description="GPA for this semester, if known, otherwise null"
    )
    target_cgpa: Optional[float] = Field(
        default=None, description="Desired CGPA after the semester, otherwise null"
    )
    total_semesters: Optional[int] = Field(
        default=None, description="Total semesters in the program, if mentioned"
    )
    completed_semesters: Optional[float] = Field(
        default=None, description="Semesters already completed, if mentioned (can be fractional)"
    )


class BotRequest(BaseModel):
    request_type: Literal["course_grade", "cgpa"] = Field(
        description="Request type: grade within a course or cumulative CGPA"
    )
    course: Optional[GradeCalculationInput] = Field(
        default=None, description="Filled in if request_type == course_grade"
    )
    cgpa: Optional[CgpaCalculationInput] = Field(
        default=None, description="Filled in if request_type == cgpa"
    )


# Standard scale: letter grade -> GPA -> percent (for course_grade mode).
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
        raise ValueError(f"don't know how to convert GPA {gpa} to a percentage (supported: {supported})")
    return GPA_TO_PERCENT[closest]


def parse_target_percent(raw: str) -> float:
    """Understands a raw percentage ("85"), a GPA ("4.0") or a letter ("A", "B+")."""
    normalized = raw.strip()
    letter_key = normalized.upper()
    if letter_key in LETTER_TO_GPA:
        return gpa_to_percent(LETTER_TO_GPA[letter_key])

    try:
        value = float(normalized.replace(",", "."))
    except ValueError:
        raise ValueError(f"couldn't recognise the target \"{raw}\"")

    # Numbers in the GPA range (max 4.0) are treated as GPA, not a percentage.
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
        raise ValueError("the final exam weight must be greater than 0%")

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
        raise ValueError("semester credits must be greater than 0")

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
            "give either the expected/earned semester GPA or the target CGPA"
        )

    result = {
        "mode": mode,
        "new_cgpa": new_cgpa,
        "needed_gpa": needed_gpa,
        "new_total_credits": new_total_credits,
    }

    # Extra context about the whole program - only if the student mentioned
    # the total number of semesters (a rough estimate that assumes an even
    # credit load across semesters).
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
        "Hi! I can calculate two kinds of study questions.\n\n"
        "1) A grade within a course - describe your graded items and target:\n"
        "\"In this course I have: assignment 1 - 18/20 (weight 10%), quiz - 8.5/10 "
        "(weight 5%), midterm - 78/100 (weight 25%). I want an A for the course.\"\n\n"
        "2) Cumulative CGPA - describe your current CGPA, completed credits and "
        "the semester:\n"
        "\"Current CGPA 3.64, 90 credits done, next semester is "
        "20 credits, what do I need to get a CGPA of 3.8?\" - or "
        "\"if I get 3.91 this semester, what will my new CGPA be?\"\n\n"
        "You can also mention the total number of semesters in your program - "
        "I'll show roughly how much this semester weighs in the whole program.",
    )


@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message) -> None:
    bot.send_chat_action(message.chat.id, "typing")

    try:
        parsed = extract_bot_request(message.text)
    except genai_errors.APIError as e:
        bot.reply_to(message, f"Gemini API error: {e}")
        return
    except Exception:
        bot.reply_to(
            message,
            "Couldn't parse the message. Make sure the text has all the "
            "numbers needed (scores, weights, credits). Try "
            "rephrasing it.",
        )
        return

    if parsed.request_type == "course_grade" and parsed.course:
        handle_course_grade(message, parsed.course)
    elif parsed.request_type == "cgpa" and parsed.cgpa:
        handle_cgpa(message, parsed.cgpa)
    else:
        bot.reply_to(
            message,
            "Couldn't tell which calculation you need. Describe either your "
            "grades within a course and the target, or your current CGPA/credits/semester.",
        )


def handle_course_grade(message: telebot.types.Message, data: GradeCalculationInput) -> None:
    try:
        result = calculate_needed_final_score(data)
    except ValueError as e:
        bot.reply_to(message, f"Couldn't calculate: {e}.")
        return

    breakdown_lines = []
    for c in data.components:
        label = c.name or "unnamed"
        contribution = c.score / c.max_score * c.weight_percent
        breakdown_lines.append(
            f"• {label}: {c.score}/{c.max_score} (weight {c.weight_percent}%) → {contribution:.1f}%"
        )

    summary = (
        "\n".join(breakdown_lines)
        + f"\n\nEarned: {result['current_total_weighted_percent']:.1f}% out of "
        f"{result['current_total_weight']:.1f}% possible\n"
        f"Final exam weight: {result['final_weight']:.1f}%\n"
        f"Target overall percentage: {result['target_percent']:.1f}%\n\n"
    )

    needed_score = result["needed"]
    if needed_score > 100:
        reply = (
            summary
            + f"You'd need {needed_score:.1f} points (out of 100) - that's above "
            "the maximum, so with this weighting the target is unfortunately "
            "out of reach."
        )
    elif needed_score <= 0:
        reply = (
            summary
            + "Target already reached - 0 points on the final would be enough "
            "(or even less, but that doesn't count)."
        )
    else:
        reply = summary + f"You need about {needed_score:.1f} points (out of 100) on the final."

    bot.reply_to(message, reply)


def handle_cgpa(message: telebot.types.Message, data: CgpaCalculationInput) -> None:
    try:
        result = calculate_cgpa(data)
    except ValueError as e:
        bot.reply_to(message, f"Couldn't calculate: {e}.")
        return

    summary = (
        f"Current CGPA: {data.current_cgpa} ({data.completed_credits:.0f} credits completed)\n"
        f"Semester credits: {data.semester_credits:.0f}\n"
        f"Total credits after the semester: {result['new_total_credits']:.0f}\n\n"
    )

    if result["mode"] == "new_cgpa":
        reply = summary + f"If you get a GPA of {data.semester_gpa} this semester, your new CGPA will be ≈ {result['new_cgpa']:.3f}."
    else:
        needed = result["needed_gpa"]
        if needed > 4.0:
            reply = (
                summary
                + f"You'd need a semester GPA of {needed:.2f} - that's above the maximum (4.0), "
                "so with this credit load the target is unfortunately "
                "out of reach in one semester."
            )
        elif needed <= 0:
            reply = (
                summary
                + "Target already reached - you just need to not fail every course this semester "
                "(by the formula even a GPA of 0 would do, but that doesn't happen in practice)."
            )
        else:
            reply = summary + f"You need a semester GPA of about {needed:.2f} for your CGPA to reach {data.target_cgpa}."

    if "total_program_credits" in result:
        reply += (
            f"\n\nAssuming an even load across semesters: the program is "
            f"~{result['total_program_credits']:.0f} credits, this semester "
            f"is ~{result['semester_weight_percent']:.1f}% of the whole program, "
            f"and ~{result['remaining_semesters_after']:.1f} semester(s) will be left after it."
        )

    bot.reply_to(message, reply)


if __name__ == "__main__":
    print("Bot is running...")
    bot.infinity_polling()
