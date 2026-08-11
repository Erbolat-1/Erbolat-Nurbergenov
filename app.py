import os
import csv
import io
import sqlite3
import secrets
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import qrcode
import telebot

from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    send_file,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ID администраторов через переменную:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

# После публикации сайта укажи HTTPS:
# например:
# https://my-rating-app.onrender.com
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://YOUR-DOMAIN.example")

DB_FILE = "rating_bot.db"

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. "
        "Создай переменную окружения BOT_TOKEN."
    )

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def db():
    connection = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_lock:
        con = db()
        cur = con.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                role TEXT DEFAULT 'user',
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                position TEXT DEFAULT '',
                department TEXT DEFAULT '',
                photo TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,

                quality INTEGER NOT NULL,
                politeness INTEGER NOT NULL,
                professionalism INTEGER NOT NULL,
                speed INTEGER NOT NULL,

                total REAL NOT NULL,
                comment TEXT DEFAULT '',

                created_at TEXT NOT NULL,

                FOREIGN KEY(employee_id)
                    REFERENCES employees(id),

                FOREIGN KEY(user_id)
                    REFERENCES users(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.commit()
        con.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_user(telegram_id):
    con = db()
    row = con.execute(
        "SELECT * FROM users WHERE telegram_id=?",
        (telegram_id,)
    ).fetchone()
    con.close()
    return row


def ensure_user(tg_user):
    existing = get_user(tg_user.id)

    full_name = " ".join(
        x for x in [
            tg_user.first_name or "",
            tg_user.last_name or ""
        ]
        if x
    ).strip()

    if not full_name:
        full_name = tg_user.username or "Пользователь"

    role = "admin" if tg_user.id in ADMIN_IDS else "user"

    with db_lock:
        con = db()

        if existing:
            con.execute("""
                UPDATE users
                SET username=?, full_name=?, role=?
                WHERE telegram_id=?
            """, (
                tg_user.username,
                full_name,
                role,
                tg_user.id
            ))
        else:
            con.execute("""
                INSERT INTO users
                (
                    telegram_id,
                    username,
                    full_name,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                tg_user.id,
                tg_user.username,
                full_name,
                role,
                now()
            ))

        con.commit()
        con.close()


def is_admin(telegram_id):
    user = get_user(telegram_id)

    if telegram_id in ADMIN_IDS:
        return True

    return bool(user and user["role"] == "admin")


def log_admin(admin_id, action):
    with db_lock:
        con = db()
        con.execute("""
            INSERT INTO admin_logs
            (admin_id, action, created_at)
            VALUES (?, ?, ?)
        """, (
            admin_id,
            action,
            now()
        ))
        con.commit()
        con.close()


def safe_delete(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def delete_later(chat_id, message_id, seconds=5):
    def worker():
        time.sleep(seconds)
        safe_delete(chat_id, message_id)

    threading.Thread(
        target=worker,
        daemon=True
    ).start()


# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def main_keyboard(user_id):
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)

    webapp_button = telebot.types.InlineKeyboardButton(
        "📱 Открыть приложение",
        web_app=telebot.types.WebAppInfo(
            url=WEBAPP_URL
        )
    )

    markup.add(webapp_button)

    markup.add(
        telebot.types.InlineKeyboardButton(
            "📝 Оценить сотрудника",
            callback_data="employees"
        ),
        telebot.types.InlineKeyboardButton(
            "📊 Статистика",
            callback_data="stats"
        )
    )

    markup.add(
        telebot.types.InlineKeyboardButton(
            "📋 Мои оценки",
            callback_data="my_ratings"
        ),
        telebot.types.InlineKeyboardButton(
            "ℹ️ Информация",
            callback_data="info"
        )
    )

    if is_admin(user_id):
        markup.add(
            telebot.types.InlineKeyboardButton(
                "👑 Админ-панель",
                callback_data="admin"
            )
        )

    return markup


def back_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()

    markup.add(
        telebot.types.InlineKeyboardButton(
            "⬅️ Назад",
            callback_data="home"
        )
    )

    return markup


# ============================================================
# START
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):

    ensure_user(message.from_user)

    # Удаляем /start
    safe_delete(
        message.chat.id,
        message.message_id
    )

    text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Это система оценки сотрудников.\n\n"
        "Выберите нужный раздел:"
    )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: True
)
def callbacks(call):

    ensure_user(call.from_user)

    chat_id = call.message.chat.id
    message_id = call.message.message_id

    try:

        # ----------------------------------------------------
        # HOME
        # ----------------------------------------------------

        if call.data == "home":

            bot.edit_message_text(
                "🏠 <b>Главное меню</b>\n\n"
                "Выберите нужный раздел:",
                chat_id,
                message_id,
                reply_markup=main_keyboard(
                    call.from_user.id
                )
            )

        # ----------------------------------------------------
        # EMPLOYEES
        # ----------------------------------------------------

        elif call.data == "employees":

            con = db()

            employees = con.execute("""
                SELECT *
                FROM employees
                WHERE active=1
                ORDER BY name
            """).fetchall()

            con.close()

            markup = telebot.types.InlineKeyboardMarkup(
                row_width=1
            )

            if not employees:

                text = (
                    "👥 <b>Сотрудники</b>\n\n"
                    "Пока сотрудники не добавлены."
                )

            else:

                text = (
                    "👥 <b>Выберите сотрудника:</b>\n\n"
                )

                for employee in employees:

                    markup.add(
                        telebot.types.InlineKeyboardButton(
                            f"👤 {employee['name']}",
                            callback_data=(
                                f"employee:{employee['id']}"
                            )
                        )
                    )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="home"
                )
            )

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup
            )

        # ----------------------------------------------------
        # EMPLOYEE
        # ----------------------------------------------------

        elif call.data.startswith("employee:"):

            employee_id = int(
                call.data.split(":")[1]
            )

            con = db()

            employee = con.execute("""
                SELECT *
                FROM employees
                WHERE id=?
            """, (
                employee_id,
            )).fetchone()

            con.close()

            if not employee:

                bot.answer_callback_query(
                    call.id,
                    "Сотрудник не найден",
                    show_alert=True
                )

                return

            markup = telebot.types.InlineKeyboardMarkup(
                row_width=1
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⭐ Оценить",
                    callback_data=(
                        f"rate:{employee_id}"
                    )
                )
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="employees"
                )
            )

            text = (
                f"👤 <b>{employee['name']}</b>\n\n"
                f"💼 Должность: "
                f"{employee['position'] or '—'}\n"
                f"🏢 Подразделение: "
                f"{employee['department'] or '—'}\n\n"
                "Выберите действие:"
            )

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup
            )

        # ----------------------------------------------------
        # RATE
        # ----------------------------------------------------

        elif call.data.startswith("rate:"):

            employee_id = int(
                call.data.split(":")[1]
            )

            con = db()

            employee = con.execute("""
                SELECT *
                FROM employees
                WHERE id=?
            """, (
                employee_id,
            )).fetchone()

            con.close()

            if not employee:
                return

            markup = telebot.types.InlineKeyboardMarkup(
                row_width=5
            )

            for rating in range(1, 6):

                markup.add(
                    telebot.types.InlineKeyboardButton(
                        "⭐" * rating,
                        callback_data=(
                            f"score:{employee_id}:{rating}"
                        )
                    )
                )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=(
                        f"employee:{employee_id}"
                    )
                )
            )

            bot.edit_message_text(
                f"⭐ <b>Оценка сотрудника</b>\n\n"
                f"👤 {employee['name']}\n\n"
                "Выберите общую оценку:",
                chat_id,
                message_id,
                reply_markup=markup
            )

        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------

        elif call.data.startswith("score:"):

            _, employee_id, score = (
                call.data.split(":")
            )

            employee_id = int(employee_id)
            score = int(score)

            # Сохраняем временный выбор в callback:
            # далее открываем Mini App для подробной оценки.

            url = (
                WEBAPP_URL
                + f"?employee={employee_id}"
                + f"&score={score}"
            )

            markup = telebot.types.InlineKeyboardMarkup()

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "📱 Продолжить оценку",
                    web_app=telebot.types.WebAppInfo(
                        url=url
                    )
                )
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"employee:{employee_id}"
                )
            )

            bot.edit_message_text(
                "⭐ <b>Продолжение оценки</b>\n\n"
                "Откройте приложение, чтобы "
                "выставить оценки по критериям "
                "и оставить комментарий.",
                chat_id,
                message_id,
                reply_markup=markup
            )

        # ----------------------------------------------------
        # STATS
        # ----------------------------------------------------

        elif call.data == "stats":

            con = db()

            total = con.execute("""
                SELECT COUNT(*)
                FROM ratings
            """).fetchone()[0]

            avg = con.execute("""
                SELECT AVG(total)
                FROM ratings
            """).fetchone()[0]

            employees = con.execute("""
                SELECT COUNT(*)
                FROM employees
                WHERE active=1
            """).fetchone()[0]

            con.close()

            avg_text = (
                f"{avg:.2f}"
                if avg is not None
                else "0.00"
            )

            bot.edit_message_text(
                "📊 <b>Общая статистика</b>\n\n"
                f"👥 Сотрудников: {employees}\n"
                f"⭐ Оценок: {total}\n"
                f"📈 Средний рейтинг: {avg_text}",
                chat_id,
                message_id,
                reply_markup=back_keyboard()
            )

        # ----------------------------------------------------
        # MY RATINGS
        # ----------------------------------------------------

        elif call.data == "my_ratings":

            user = get_user(
                call.from_user.id
            )

            con = db()

            rows = con.execute("""
                SELECT
                    ratings.*,
                    employees.name AS employee_name
                FROM ratings
                JOIN employees
                    ON employees.id=ratings.employee_id
                WHERE ratings.user_id=?
                ORDER BY ratings.created_at DESC
                LIMIT 10
            """, (
                user["id"],
            )).fetchall()

            con.close()

            if not rows:

                text = (
                    "📋 <b>Мои оценки</b>\n\n"
                    "Вы ещё не оставляли оценок."
                )

            else:

                text = "📋 <b>Последние оценки</b>\n\n"

                for row in rows:

                    text += (
                        f"👤 {row['employee_name']}\n"
                        f"⭐ {row['total']:.1f}/5\n"
                        f"📅 {row['created_at']}\n\n"
                    )

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=back_keyboard()
            )

        # ----------------------------------------------------
        # INFO
        # ----------------------------------------------------

        elif call.data == "info":

            bot.edit_message_text(
                "ℹ️ <b>О системе</b>\n\n"
                "Система предназначена для "
                "электронной оценки сотрудников.\n\n"
                "Оценка сохраняется в базе данных "
                "и используется для формирования "
                "статистики.",
                chat_id,
                message_id,
                reply_markup=back_keyboard()
            )

        # ----------------------------------------------------
        # ADMIN
        # ----------------------------------------------------

        elif call.data == "admin":

            if not is_admin(call.from_user.id):

                bot.answer_callback_query(
                    call.id,
                    "Доступ запрещён",
                    show_alert=True
                )

                return

            markup = telebot.types.InlineKeyboardMarkup(
                row_width=2
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "👥 Сотрудники",
                    callback_data="admin_employees"
                ),
                telebot.types.InlineKeyboardButton(
                    "📊 Статистика",
                    callback_data="stats"
                )
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⭐ Последние оценки",
                    callback_data="admin_ratings"
                ),
                telebot.types.InlineKeyboardButton(
                    "📥 CSV",
                    callback_data="admin_csv"
                )
            )

            markup.add(
                telebot.types.InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data="home"
                )
            )

            bot.edit_message_text(
                "👑 <b>Админ-панель</b>\n\n"
                "Выберите раздел:",
                chat_id,
                message_id,
                reply_markup=markup
            )

        # ----------------------------------------------------
        # ADMIN EMPLOYEES
        # ----------------------------------------------------

        elif call.data == "admin_employees":

            if not is_admin(call.from_user.id):
                return

            con = db()

            employees = con.execute("""
                SELECT *
                FROM employees
                ORDER BY active DESC, name
            """).fetchall()

            con.close()

            text = "👥 <b>Сотрудники</b>\n\n"

            if not employees:
                text += "Сотрудников нет."

            else:

                for employee in employees:

                    status = (
                        "🟢"
                        if employee["active"]
                        else "🔴"
                    )

                    text += (
                        f"{status} "
                        f"<b>{employee['name']}</b>\n"
                        f"💼 {employee['position'] or '—'}\n\n"
                    )

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=back_keyboard()
            )

        # ----------------------------------------------------
        # ADMIN RATINGS
        # ----------------------------------------------------

        elif call.data == "admin_ratings":

            if not is_admin(call.from_user.id):
                return

            con = db()

            rows = con.execute("""
                SELECT
                    ratings.*,
                    employees.name AS employee_name,
                    users.full_name AS user_name
                FROM ratings
                JOIN employees
                    ON employees.id=ratings.employee_id
                JOIN users
                    ON users.id=ratings.user_id
                ORDER BY ratings.created_at DESC
                LIMIT 10
            """).fetchall()

            con.close()

            text = "⭐ <b>Последние оценки</b>\n\n"

            if not rows:

                text += "Оценок пока нет."

            else:

                for row in rows:

                    text += (
                        f"👤 <b>{row['employee_name']}</b>\n"
                        f"⭐ {row['total']:.1f}/5\n"
                        f"🧑 {row['user_name']}\n"
                        f"📅 {row['created_at']}\n"
                    )

                    if row["comment"]:
                        text += (
                            f"💬 {row['comment'][:150]}\n"
                        )

                    text += "\n"

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=back_keyboard()
            )

        # ----------------------------------------------------
        # CSV
        # ----------------------------------------------------

        elif call.data == "admin_csv":

            if not is_admin(call.from_user.id):
                return

            con = db()

            rows = con.execute("""
                SELECT
                    ratings.id,
                    employees.name AS employee,
                    users.full_name AS user_name,
                    ratings.quality,
                    ratings.politeness,
                    ratings.professionalism,
                    ratings.speed,
                    ratings.total,
                    ratings.comment,
                    ratings.created_at
                FROM ratings
                JOIN employees
                    ON employees.id=ratings.employee_id
                JOIN users
                    ON users.id=ratings.user_id
                ORDER BY ratings.created_at DESC
            """).fetchall()

            con.close()

            output = io.StringIO()

            writer = csv.writer(output)

            writer.writerow([
                "ID",
                "Сотрудник",
                "Пользователь",
                "Качество",
                "Вежливость",
                "Профессионализм",
                "Скорость",
                "Итог",
                "Комментарий",
                "Дата"
            ])

            for row in rows:

                writer.writerow([
                    row["id"],
                    row["employee"],
                    row["user_name"],
                    row["quality"],
                    row["politeness"],
                    row["professionalism"],
                    row["speed"],
                    row["total"],
                    row["comment"],
                    row["created_at"]
                ])

            file_data = io.BytesIO(
                output.getvalue().encode("utf-8-sig")
            )

            file_data.seek(0)

            bot.send_document(
                chat_id,
                telebot.types.InputFile(
                    file_data,
                    filename="ratings.csv"
                )
            )

            log_admin(
                call.from_user.id,
                "Экспорт оценок CSV"
            )

        bot.answer_callback_query(call.id)

    except Exception as e:

        print("CALLBACK ERROR:", e)

        try:
            bot.answer_callback_query(
                call.id,
                "Произошла ошибка",
                show_alert=True
            )
        except Exception:
            pass


# ============================================================
# MINI APP HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="
        width=device-width,
        initial-scale=1,
        maximum-scale=1,
        user-scalable=no
    "
>

<title>Оценка сотрудника</title>

<script src="https://telegram.org/js/telegram-web-app.js"></script>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

    background:
        linear-gradient(
            145deg,
            #06152f,
            #0b2d61
        );

    color: white;
    min-height: 100vh;
}

.container {
    width: 100%;
    max-width: 520px;
    margin: auto;
    padding: 18px;
}

.header {
    text-align: center;
    padding: 10px 0 20px;
}

.logo {
    width: 64px;
    height: 64px;

    margin: auto;

    border-radius: 50%;

    display: flex;
    align-items: center;
    justify-content: center;

    background: white;
    color: #0b4ea2;

    font-size: 30px;
}

h1 {
    margin: 12px 0 4px;
    font-size: 24px;
}

.subtitle {
    opacity: .75;
}

.card {
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.12);

    border-radius: 20px;

    padding: 18px;

    margin-bottom: 14px;

    backdrop-filter: blur(14px);
}

.employee {
    text-align: center;
}

.employee-name {
    font-size: 22px;
    font-weight: 700;
}

.employee-position {
    opacity: .7;
    margin-top: 5px;
}

.criterion {
    margin-top: 18px;
}

.criterion-title {
    font-weight: 600;
    margin-bottom: 8px;
}

.stars {
    display: flex;
    gap: 6px;
}

.star {
    flex: 1;

    border: none;

    border-radius: 12px;

    padding: 11px 5px;

    font-size: 20px;

    background: rgba(255,255,255,.10);

    color: white;
}

.star.active {
    background: #f5c542;
    color: #111;
}

textarea {
    width: 100%;

    min-height: 110px;

    resize: vertical;

    border: none;

    outline: none;

    border-radius: 14px;

    padding: 14px;

    font-size: 16px;

    background: rgba(255,255,255,.10);

    color: white;
}

textarea::placeholder {
    color: rgba(255,255,255,.55);
}

button.submit {
    width: 100%;

    border: none;

    border-radius: 16px;

    padding: 15px;

    font-size: 17px;

    font-weight: 700;

    background: #ffffff;

    color: #0b3b78;

    margin-top: 15px;
}

button.submit:disabled {
    opacity: .5;
}

.message {
    text-align: center;
    padding: 30px 15px;
}

.success {
    font-size: 60px;
}

</style>

</head>

<body>

<div class="container">

    <div class="header">

        <div class="logo">
            ⭐
        </div>

        <h1>
            Оценка сотрудника
        </h1>

        <div class="subtitle">
            Ваша оценка помогает улучшать качество работы
        </div>

    </div>


    <div id="app">

        <div class="card employee">

            <div id="employeeName">
                Загрузка...
            </div>

            <div
                class="employee-position"
                id="employeePosition">
            </div>

        </div>


        <div class="card">

            <div id="criteria"></div>

        </div>


        <div class="card">

            <div class="criterion-title">
                💬 Комментарий
            </div>

            <textarea
                id="comment"
                maxlength="1000"
                placeholder="Напишите комментарий...">
            </textarea>

        </div>


        <button
            class="submit"
            id="submit"
            onclick="submitRating()">

            ✅ Отправить оценку

        </button>

    </div>


    <div
        id="success"
        class="card message"
        style="display:none;">

        <div class="success">
            ✅
        </div>

        <h2>
            Спасибо!
        </h2>

        <p>
            Ваша оценка успешно сохранена.
        </p>

    </div>

</div>


<script>

const tg = window.Telegram.WebApp;

tg.ready();
tg.expand();


let employeeId = null;

let scores = {
    quality: 0,
    politeness: 0,
    professionalism: 0,
    speed: 0
};


const params = new URLSearchParams(
    window.location.search
);

employeeId = params.get("employee");


const criteria = [
    {
        key: "quality",
        title: "Качество работы"
    },
    {
        key: "politeness",
        title: "Вежливость"
    },
    {
        key: "professionalism",
        title: "Профессионализм"
    },
    {
        key: "speed",
        title: "Скорость работы"
    }
];


function createCriteria() {

    const container =
        document.getElementById("criteria");

    container.innerHTML = "";

    criteria.forEach(item => {

        const wrapper =
            document.createElement("div");

        wrapper.className = "criterion";

        wrapper.innerHTML = `
            <div class="criterion-title">
                ${item.title}
            </div>

            <div class="stars">

                ${[1,2,3,4,5].map(n => `
                    <button
                        class="star"
                        id="${item.key}-${n}"
                        onclick="
                            setScore(
                                '${item.key}',
                                ${n}
                            )
                        ">
                        ${n}
                    </button>
                `).join("")}

            </div>
        `;

        container.appendChild(wrapper);

    });

}


function setScore(key, value) {

    scores[key] = value;

    for (
        let i = 1;
        i <= 5;
        i++
    ) {

        const element =
            document.getElementById(
                `${key}-${i}`
            );

        if (i <= value) {
            element.classList.add("active");
        } else {
            element.classList.remove("active");
        }

    }

}


async function loadEmployee() {

    if (!employeeId) {

        document.getElementById(
            "employeeName"
        ).innerText =
            "Сотрудник не выбран";

        return;
    }

    try {

        const response =
            await fetch(
                `/api/employee/${employeeId}`
            );

        const data =
            await response.json();

        if (!data.success) {

            document.getElementById(
                "employeeName"
            ).innerText =
                "Сотрудник не найден";

            return;
        }

        document.getElementById(
            "employeeName"
        ).innerText =
            data.employee.name;

        document.getElementById(
            "employeePosition"
        ).innerText =
            data.employee.position || "";

    } catch (error) {

        console.error(error);

    }

}


async function submitRating() {

    const submit =
        document.getElementById("submit");

    if (
        !scores.quality ||
        !scores.politeness ||
        !scores.professionalism ||
        !scores.speed
    ) {

        tg.showAlert(
            "Поставьте оценку по всем критериям."
        );

        return;
    }


    submit.disabled = true;


    const user =
        tg.initDataUnsafe.user || {};


    const payload = {

        employee_id:
            Number(employeeId),

        telegram_id:
            user.id || 0,

        init_data:
            tg.initData || "",

        quality:
            scores.quality,

        politeness:
            scores.politeness,

        professionalism:
            scores.professionalism,

        speed:
            scores.speed,

        comment:
            document.getElementById(
                "comment"
            ).value.trim()

    };


    try {

        const response =
            await fetch(
                "/api/rating",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body:
                        JSON.stringify(payload)
                }
            );


        const result =
            await response.json();


        if (!result.success) {

            tg.showAlert(
                result.error ||
                "Не удалось сохранить оценку."
            );

            submit.disabled = false;

            return;
        }


        document.getElementById(
            "app"
        ).style.display = "none";


        document.getElementById(
            "success"
        ).style.display = "block";


        tg.HapticFeedback.notificationOccurred(
            "success"
        );


    } catch (error) {

        console.error(error);

        tg.showAlert(
            "Ошибка соединения с сервером."
        );

        submit.disabled = false;

    }

}


createCriteria();

loadEmployee();

</script>

</body>
</html>
"""


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/")
def index():

    return render_template_string(
        HTML
    )


@app.route("/api/employee/<int:employee_id>")
def api_employee(employee_id):

    con = db()

    employee = con.execute("""
        SELECT
            id,
            name,
            position,
            department,
            photo
        FROM employees
        WHERE id=? AND active=1
    """, (
        employee_id,
    )).fetchone()

    con.close()

    if not employee:

        return jsonify({
            "success": False,
            "error": "Сотрудник не найден"
        }), 404

    return jsonify({
        "success": True,
        "employee": dict(employee)
    })


# ============================================================
# TELEGRAM WEBAPP VALIDATION
# ============================================================

def validate_telegram_webapp(init_data):

    """
    Проверяет подпись Telegram Web App.

    Если init_data пустой — запрос отклоняется.
    """

    if not init_data:
        return False

    try:

        from urllib.parse import (
            parse_qsl,
            urlencode
        )

        import hashlib
        import hmac

        parsed = dict(
            parse_qsl(
                init_data,
                keep_blank_values=True
            )
        )

        received_hash = parsed.pop(
            "hash",
            None
        )

        if not received_hash:
            return False

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value
            in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash
        ):
            return False

        # Проверяем свежесть данных.
        auth_date = int(
            parsed.get("auth_date", "0")
        )

        if (
            auth_date
            and time.time() - auth_date > 86400
        ):
            return False

        return True

    except Exception as e:

        print(
            "WEBAPP VALIDATION ERROR:",
            e
        )

        return False


# ============================================================
# SAVE RATING
# ============================================================

@app.route(
    "/api/rating",
    methods=["POST"]
)
def api_rating():

    data = request.get_json(
        silent=True
    ) or {}


    init_data = data.get(
        "init_data",
        ""
    )


    if not validate_telegram_webapp(
        init_data
    ):

        return jsonify({
            "success": False,
            "error": "Недействительные данные Telegram."
        }), 403


    try:

        employee_id = int(
            data["employee_id"]
        )

        telegram_id = int(
            data["telegram_id"]
        )

        quality = int(
            data["quality"]
        )

        politeness = int(
            data["politeness"]
        )

        professionalism = int(
            data["professionalism"]
        )

        speed = int(
            data["speed"]
        )

        comment = str(
            data.get("comment", "")
        ).strip()[:1000]

    except Exception:

        return jsonify({
            "success": False,
            "error": "Некорректные данные."
        }), 400


    scores = [
        quality,
        politeness,
        professionalism,
        speed
    ]


    if any(
        x < 1 or x > 5
        for x in scores
    ):

        return jsonify({
            "success": False,
            "error": "Оценка должна быть от 1 до 5."
        }), 400


    con = db()


    employee = con.execute("""
        SELECT *
        FROM employees
        WHERE id=? AND active=1
    """, (
        employee_id,
    )).fetchone()


    if not employee:

        con.close()

        return jsonify({
            "success": False,
            "error": "Сотрудник не найден."
        }), 404


    user = con.execute("""
        SELECT *
        FROM users
        WHERE telegram_id=?
    """, (
        telegram_id,
    )).fetchone()


    if not user:

        con.execute("""
            INSERT INTO users
            (
                telegram_id,
                username,
                full_name,
                role,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            telegram_id,
            "",
            "Telegram пользователь",
            "user",
            now()
        ))

        con.commit()

        user = con.execute("""
            SELECT *
            FROM users
            WHERE telegram_id=?
        """, (
            telegram_id,
        )).fetchone()


    # Защита от многократной оценки одного
    # сотрудника одним пользователем.
    existing = con.execute("""
        SELECT id
        FROM ratings
        WHERE employee_id=?
          AND user_id=?
    """, (
        employee_id,
        user["id"]
    )).fetchone()


    if existing:

        con.close()

        return jsonify({
            "success": False,
            "error": "Вы уже оценивали этого сотрудника."
        }), 409


    total = round(
        (
            quality
            + politeness
            + professionalism
            + speed
        ) / 4,
        2
    )


    con.execute("""
        INSERT INTO ratings
        (
            employee_id,
            user_id,
            quality,
            politeness,
            professionalism,
            speed,
            total,
            comment,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        employee_id,
        user["id"],
        quality,
        politeness,
        professionalism,
        speed,
        total,
        comment,
        now()
    ))


    con.commit()
    con.close()


    # Уведомление администраторам.
    for admin_id in ADMIN_IDS:

        try:

            bot.send_message(
                admin_id,
                "⭐ <b>Новая оценка</b>\n\n"
                f"👤 Сотрудник: "
                f"<b>{employee['name']}</b>\n"
                f"⭐ Итог: <b>{total}/5</b>\n"
                f"💬 {comment or 'Без комментария'}"
            )

        except Exception as e:

            print(
                "ADMIN NOTIFICATION ERROR:",
                e
            )


    return jsonify({
        "success": True,
        "total": total
    })


# ============================================================
# ADMIN API
# ============================================================

def require_admin_from_header():

    telegram_id = request.headers.get(
        "X-Telegram-ID"
    )

    if not telegram_id:
        return False

    try:
        return is_admin(
            int(telegram_id)
        )
    except Exception:
        return False


@app.route("/admin")
def admin_page():

    return """
    <html>
    <head>
        <meta charset="UTF-8">
        <meta
            name="viewport"
            content="width=device-width,
                     initial-scale=1"
        >

        <title>Админ-панель</title>

        <style>

        body {
            font-family: Arial;
            margin: 0;
            background: #07152d;
            color: white;
        }

        .container {
            max-width: 1100px;
            margin: auto;
            padding: 20px;
        }

        .card {
            background: #102a52;
            border-radius: 18px;
            padding: 20px;
            margin-bottom: 15px;
        }

        h1 {
            margin-top: 0;
        }

        .grid {
            display: grid;
            grid-template-columns:
                repeat(auto-fit,minmax(180px,1fr));
            gap: 15px;
        }

        .number {
            font-size: 34px;
            font-weight: bold;
        }

        </style>

    </head>

    <body>

        <div class="container">

            <div class="card">

                <h1>
                    👑 Админ-панель
                </h1>

                <p>
                    Статистика системы оценки сотрудников
                </p>

            </div>

            <div
                class="grid"
                id="stats">
            </div>

        </div>

        <script>

        async function load() {

            const response =
                await fetch("/api/admin/stats");

            const data =
                await response.json();

            if (!data.success) {

                document.body.innerHTML =
                    "<h2>Доступ запрещён</h2>";

                return;
            }

            document.getElementById(
                "stats"
            ).innerHTML = `

                <div class="card">

                    <div>
                        👥 Сотрудники
                    </div>

                    <div class="number">
                        ${data.employees}
                    </div>

                </div>

                <div class="card">

                    <div>
                        ⭐ Оценки
                    </div>

                    <div class="number">
                        ${data.ratings}
                    </div>

                </div>

                <div class="card">

                    <div>
                        📈 Средний рейтинг
                    </div>

                    <div class="number">
                        ${data.average}
                    </div>

                </div>

                <div class="card">

                    <div>
                        👤 Пользователи
                    </div>

                    <div class="number">
                        ${data.users}
                    </div>

                </div>

            `;

        }

        load();

        </script>

    </body>
    </html>
    """


@app.route("/api/admin/stats")
def admin_stats():

    if not require_admin_from_header():

        return jsonify({
            "success": False
        }), 403

    con = db()

    employees = con.execute("""
        SELECT COUNT(*)
        FROM employees
        WHERE active=1
    """).fetchone()[0]

    ratings = con.execute("""
        SELECT COUNT(*)
        FROM ratings
    """).fetchone()[0]

    users = con.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    average = con.execute("""
        SELECT AVG(total)
        FROM ratings
    """).fetchone()[0]

    con.close()

    return jsonify({
        "success": True,
        "employees": employees,
        "ratings": ratings,
        "users": users,
        "average": round(
            average or 0,
            2
        )
    })


# ============================================================
# ADD EMPLOYEE API
# ============================================================

@app.route(
    "/api/admin/employee",
    methods=["POST"]
)
def add_employee():

    if not require_admin_from_header():

        return jsonify({
            "success": False,
            "error": "Доступ запрещён"
        }), 403


    data = request.get_json(
        silent=True
    ) or {}


    name = str(
        data.get("name", "")
    ).strip()

    position = str(
        data.get("position", "")
    ).strip()

    department = str(
        data.get("department", "")
    ).strip()


    if not name:

        return jsonify({
            "success": False,
            "error": "Укажите имя сотрудника."
        }), 400


    with db_lock:

        con = db()

        cursor = con.execute("""
            INSERT INTO employees
            (
                name,
                position,
                department,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            name,
            position,
            department,
            now()
        ))

        employee_id = cursor.lastrowid

        con.commit()
        con.close()


    return jsonify({
        "success": True,
        "id": employee_id
    })


# ============================================================
# QR CODE
# ============================================================

@app.route(
    "/qr/<int:employee_id>"
)
def qr_code(employee_id):

    url = (
        WEBAPP_URL
        + f"?employee={employee_id}"
    )

    image = qrcode.make(url)

    output = io.BytesIO()

    image.save(
        output,
        format="PNG"
    )

    output.seek(0)

    return send_file(
        output,
        mimetype="image/png",
        download_name=(
            f"employee_{employee_id}.png"
        )
    )


# ============================================================
# RUN FLASK
# ============================================================

def run_flask():

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False
    )


# ============================================================
# RUN BOT
# ============================================================

def run_bot():

    print("Telegram bot started.")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("=" * 60)

    print(
        "EMPLOYEE RATING BOT"
    )

    print("=" * 60)

    print(
        f"WebApp URL: {WEBAPP_URL}"
    )

    print(
        f"Port: {PORT}"
    )

    print(
        f"Admins: {ADMIN_IDS}"
    )

    print("=" * 60)


    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()


    run_bot()
