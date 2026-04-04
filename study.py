from pathlib import Path
from datetime import date, datetime, timedelta
import os
import re
from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from google import genai
from google.genai import types
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import inspect, text
from starlette.middleware.wsgi import WSGIMiddleware
import PyPDF2
from werkzeug.utils import secure_filename


from models import ActivePomodoroSession, ActiveStudySession, PomodoroSession, StudyPlan, StudyPlanEntry, StudySession, Subject, Task, User, db

# Load environment settings and configure file storage paths.
BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = BASE_DIR / "instance" / "database.db"
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
load_dotenv(BASE_DIR / ".env")
UPLOAD_FOLDER = BASE_DIR / "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Configure the main Flask application and SQLite connection.
flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = "secret123"
flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DATABASE_PATH.as_posix()}"
flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
flask_app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

db.init_app(flask_app)

# Configure login/session management for authenticated pages.
login_manager = LoginManager()
login_manager.init_app(flask_app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Create database tables and backfill any newer SQLite columns if needed.
with flask_app.app_context():
    db.create_all()
    inspector = inspect(db.engine)
    task_columns = {column["name"] for column in inspector.get_columns("task")}
    if "estimated_hours_per_task" not in task_columns:
        db.session.execute(
            text("ALTER TABLE task ADD COLUMN estimated_hours_per_task INTEGER NOT NULL DEFAULT 1")
        )
        db.session.commit()
    if "is_completed" not in task_columns:
        db.session.execute(
            text("ALTER TABLE task ADD COLUMN is_completed BOOLEAN NOT NULL DEFAULT 0")
        )
        db.session.commit()


# Parse task deadlines safely so planner generation does not crash on bad dates.
def parse_deadline(deadline_value):
    if not deadline_value:
        return date.today() + timedelta(days=7)

    try:
        return datetime.strptime(deadline_value, "%Y-%m-%d").date()
    except ValueError:
        return date.today() + timedelta(days=7)


# Build a day-by-day study schedule from the user's pending tasks.
def build_study_plan(tasks, daily_hours):
    today = date.today()
    task_queue = sorted(tasks, key=lambda item: (parse_deadline(item.deadline), item.id))
    plan = {}
    unscheduled_tasks = []

    for task in task_queue:
        deadline_date = max(parse_deadline(task.deadline), today)
        hours_left = max(task.estimated_hours_per_task or 1, 1)
        current_day = today
        scheduled = 0

        while current_day <= deadline_date and hours_left > 0:
            day_key = current_day.isoformat()
            plan.setdefault(day_key, {"remaining": daily_hours, "items": []})
            available_hours = plan[day_key]["remaining"]

            if available_hours > 0:
                assigned_hours = min(available_hours, hours_left)
                plan[day_key]["items"].append(
                    {
                        "task_title": task.title,
                        "subject_name": task.subject.name,
                        "hours": assigned_hours,
                        "deadline": task.deadline or "Flexible",
                    }
                )
                plan[day_key]["remaining"] -= assigned_hours
                hours_left -= assigned_hours
                scheduled += assigned_hours

            current_day += timedelta(days=1)

        if hours_left > 0:
            unscheduled_tasks.append(
                {
                    "title": task.title,
                    "subject_name": task.subject.name,
                    "missing_hours": hours_left,
                    "deadline": task.deadline or "Flexible",
                }
            )

    formatted_plan = []
    for day_key in sorted(plan):
        day_date = datetime.strptime(day_key, "%Y-%m-%d").date()
        formatted_plan.append(
            {
                "date": day_date.strftime("%b %d, %Y"),
                "entries": plan[day_key]["items"],
                "used_hours": daily_hours - plan[day_key]["remaining"],
                "remaining_hours": plan[day_key]["remaining"],
            }
        )

    return formatted_plan, unscheduled_tasks


# Rebuild saved study plans into the dashboard/planner display format.
def group_saved_entries(entries, daily_hours):
    grouped = {}

    for entry in entries:
        grouped.setdefault(entry.study_date, []).append(entry)

    formatted_plan = []
    for study_date in sorted(grouped):
        day_entries = grouped[study_date]
        used_hours = sum(entry.hours for entry in day_entries)
        formatted_plan.append(
            {
                "date": datetime.strptime(study_date, "%Y-%m-%d").strftime("%b %d, %Y"),
                "entries": [
                    {
                        "task_title": entry.task_title,
                        "subject_name": entry.subject_name,
                        "hours": entry.hours,
                        "deadline": entry.deadline,
                    }
                    for entry in day_entries
                ],
                "used_hours": used_hours,
                "remaining_hours": max(daily_hours - used_hours, 0),
            }
        )

    return formatted_plan


# Prepare the coaching prompt that TutorAi sends to Gemini.
def build_ai_plan_prompt(tasks, subject_minutes):
    task_lines = [
        f"- {task.title} | Subject: {task.subject.name} | Deadline: {task.deadline or 'No deadline'} | Estimated Hours: {task.estimated_hours_per_task}"
        for task in tasks
    ] or ["- No tasks yet"]

    minute_lines = [
        f"- {subject_name}: {minutes} minutes studied"
        for subject_name, minutes in subject_minutes.items()
    ] or ["- No study sessions tracked yet"]

    return (
        "Review the student's task list and tracked study time.\n"
        "Give a short coaching update with:\n"
        "1. Current weak area\n"
        "2. What subject needs more attention tomorrow\n"
        "3. One practical next action\n\n"
        "Tasks:\n"
        + "\n".join(task_lines)
        + "\n\nStudy Time:\n"
        + "\n".join(minute_lines)
    )


# Return a local study suggestion when Gemini is unavailable.
def build_fallback_ai_update(tasks, subject_minutes):
    if not tasks:
        return "You have no tasks yet. Add a few tasks first, then I can suggest where to focus next."

    subject_task_counts = {}
    for task in tasks:
        subject_task_counts[task.subject.name] = subject_task_counts.get(task.subject.name, 0) + 1

    ranked_subjects = sorted(
        subject_task_counts,
        key=lambda name: (subject_minutes.get(name, 0), -subject_task_counts[name])
    )
    target_subject = ranked_subjects[0]
    target_minutes = subject_minutes.get(target_subject, 0)
    task_count = subject_task_counts[target_subject]

    return (
        f"You seem weakest in {target_subject} right now. "
        f"You have {task_count} pending task(s) there and only {target_minutes} tracked study minute(s). "
        f"Spend more time on {target_subject} tomorrow and start with the nearest deadline first."
    )


# Create the Gemini client from environment configuration.
def get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(client_args={"trust_env": False}),
    )


# Convert stored timestamp strings back into datetime values.
def parse_timestamp(timestamp_value):
    return datetime.strptime(timestamp_value, "%Y-%m-%d %H:%M:%S")


# Extract text from typed PDFs before falling back to multimodal Gemini analysis.
def extract_pdf_text(file_path):
    extracted_pages = []

    with open(file_path, "rb") as pdf_file:
        reader = PyPDF2.PdfReader(pdf_file)
        for page in reader.pages:
            extracted_pages.append(page.extract_text() or "")

    return "\n".join(extracted_pages).strip()


def clean_ai_response_text(text):
    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*(.*?)\*", r"\1", cleaned)
    cleaned = re.sub(r"`{1,3}", "", cleaned)
    cleaned = re.sub(r"(?m)^\s*[*]\s+", "- ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def get_ai_chat_turns():
    return session.get("ai_chat_turns", [])


def save_ai_chat_turns(turns):
    session["ai_chat_turns"] = turns
    session.modified = True


def build_chat_prompt(turns, user_input):
    transcript = []
    for turn in turns:
        transcript.append(f"User: {turn['question']}")
        transcript.append(f"TutorAi: {turn['answer']}")

    transcript.append(f"User: {user_input}")
    transcript.append("TutorAi:")
    return (
        "You are TutorAi, a helpful study assistant. Continue the conversation naturally.\n"
        "Use the earlier messages as context when the user's next question depends on them.\n"
        "Be clear, accurate, and student-friendly.\n\n"
        + "\n".join(transcript)
    )


# Let Gemini read uploaded PDFs or images, including handwritten notes.
def analyze_notes_with_gemini(file_path, mime_type):
    client = get_gemini_client()
    with open(file_path, "rb") as uploaded_file:
        file_bytes = uploaded_file.read()

    completion = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_text(
                text=(
                    "Analyze these study notes. They may be typed, handwritten, scanned, or image-based.\n"
                    "Extract readable content as best as possible.\n\n"
                    "Return exactly three sections:\n"
                    "1. Summary\n"
                    "2. Key Points\n"
                    "3. 5 Quiz Questions"
                )
            ),
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        ],
    )
    return clean_ai_response_text(completion.text)


@flask_app.route("/")
def home():
    return render_template("home.html")


@flask_app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("signup.html")

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash("That username is already taken.", "error")
            return render_template("signup.html", username=username)

        hashed_password = generate_password_hash(password)
        user = User(username=username, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


@flask_app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        user = User.query.filter_by(username=username).first()

        if not user:
            flash("Invalid credentials: user not found.", "error")
            return render_template("login.html", username=username)

        if not check_password_hash(user.password, password):
            flash("Invalid credentials: wrong password.", "error")
            return render_template("login.html", username=username)

        login_user(user)
        flash("Welcome back.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@flask_app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@flask_app.route("/dashboard")
@login_required
def dashboard():
    subjects = Subject.query.filter_by(user_id=current_user.id).order_by(Subject.name.asc()).all()
    tasks = (
        Task.query.join(Subject)
        .filter(Subject.user_id == current_user.id)
        .order_by(Task.is_completed.asc(), Task.deadline.asc(), Task.id.asc())
        .all()
    )
    saved_plans_count = StudyPlan.query.filter_by(user_id=current_user.id).count()
    sessions = (
        StudySession.query.join(Subject)
        .filter(StudySession.user_id == current_user.id)
        .order_by(StudySession.id.desc())
        .limit(5)
        .all()
    )
    subject_minutes = {}
    for session in sessions:
        subject_minutes[session.subject.name] = subject_minutes.get(session.subject.name, 0) + session.minutes
    active_session = ActiveStudySession.query.filter_by(user_id=current_user.id).first()
    active_pomodoro = ActivePomodoroSession.query.filter_by(user_id=current_user.id).first()
    pomodoro_sessions = (
        PomodoroSession.query.filter_by(user_id=current_user.id)
        .order_by(PomodoroSession.id.desc())
        .limit(5)
        .all()
    )
    return render_template(
        "dashboard.html",
        subjects=subjects,
        tasks=tasks,
        saved_plans_count=saved_plans_count,
        sessions=sessions,
        subject_minutes=subject_minutes,
        active_session=active_session,
        active_pomodoro=active_pomodoro,
        pomodoro_sessions=pomodoro_sessions,
    )


@flask_app.route("/health")
def health():
    users = User.query.count()
    subjects = Subject.query.count()
    tasks = Task.query.count()
    return {
        "status": "ok",
        "users": users,
        "subjects": subjects,
        "tasks": tasks,
    }


@flask_app.route("/add-subject", methods=["GET", "POST"])
@login_required
def add_subject():
    if request.method == "POST":
        name = request.form["name"].strip()

        if not name:
            flash("Subject name is required.", "error")
            return render_template("add_subject.html")

        subject = Subject(name=name, user_id=current_user.id)
        db.session.add(subject)
        db.session.commit()
        flash("Subject added successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_subject.html")


@flask_app.route("/add-task", methods=["GET", "POST"])
@login_required
def add_task():
    subjects = Subject.query.filter_by(user_id=current_user.id).order_by(Subject.name.asc()).all()

    if request.method == "POST":
        title = request.form["title"].strip()
        deadline = request.form["deadline"].strip()
        subject_id = request.form["subject_id"]
        estimated_hours_raw = request.form["estimated_hours_per_task"].strip()

        if not title or not subject_id or not estimated_hours_raw:
            flash("Task title, estimated hours, and subject are required.", "error")
            return render_template("add_task.html", subjects=subjects)

        try:
            estimated_hours = int(estimated_hours_raw)
        except ValueError:
            flash("Estimated hours must be a whole number.", "error")
            return render_template("add_task.html", subjects=subjects)

        if estimated_hours < 1:
            flash("Estimated hours must be at least 1.", "error")
            return render_template("add_task.html", subjects=subjects)

        subject = Subject.query.filter_by(id=subject_id, user_id=current_user.id).first()
        if not subject:
            flash("Please select a valid subject.", "error")
            return render_template("add_task.html", subjects=subjects)

        task = Task(
            title=title,
            deadline=deadline or None,
            estimated_hours_per_task=estimated_hours,
            is_completed=False,
            subject_id=subject.id,
        )
        db.session.add(task)
        db.session.commit()
        flash("Task added successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("add_task.html", subjects=subjects)


@flask_app.route("/edit-task/<int:task_id>", methods=["GET", "POST"])
@login_required
def edit_task(task_id):
    task = (
        Task.query.join(Subject)
        .filter(Task.id == task_id, Subject.user_id == current_user.id)
        .first_or_404()
    )

    if request.method == "POST":
        title = request.form["title"].strip()
        deadline = request.form["deadline"].strip()
        estimated_hours_raw = request.form["estimated_hours_per_task"].strip()
        is_completed = request.form.get("is_completed") == "on"

        if not title or not estimated_hours_raw:
            flash("Task title and estimated hours are required.", "error")
            return render_template("edit_task.html", task=task)

        try:
            estimated_hours = int(estimated_hours_raw)
        except ValueError:
            flash("Estimated hours must be a whole number.", "error")
            return render_template("edit_task.html", task=task)

        if estimated_hours < 1:
            flash("Estimated hours must be at least 1.", "error")
            return render_template("edit_task.html", task=task)

        task.title = title
        task.deadline = deadline or None
        task.estimated_hours_per_task = estimated_hours
        task.is_completed = is_completed
        db.session.commit()
        flash("Task updated successfully.", "success")
        return redirect(url_for("dashboard"))

    return render_template("edit_task.html", task=task)


@flask_app.route("/planner", methods=["GET", "POST"])
@login_required
def planner():
    tasks = (
        Task.query.join(Subject)
        .filter(Subject.user_id == current_user.id)
        .order_by(Task.deadline.asc(), Task.id.asc())
        .all()
    )

    if request.method == "POST":
        daily_hours_raw = request.form["daily_hours"].strip()

        try:
            daily_hours = int(daily_hours_raw)
        except ValueError:
            flash("Daily available hours must be a whole number.", "error")
            return render_template("planner_form.html")

        if daily_hours < 1:
            flash("Daily available hours must be at least 1.", "error")
            return render_template("planner_form.html")

        if not tasks:
            flash("Add some tasks before generating a planner.", "error")
            return render_template("planner_form.html")

        plan, unscheduled_tasks = build_study_plan(tasks, daily_hours)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        saved_plan = StudyPlan(
            title=f"Plan {created_at}",
            daily_hours=daily_hours,
            created_at=created_at,
            user_id=current_user.id,
        )
        db.session.add(saved_plan)
        db.session.flush()

        for day in plan:
            study_date = datetime.strptime(day["date"], "%b %d, %Y").strftime("%Y-%m-%d")
            for entry in day["entries"]:
                db.session.add(
                    StudyPlanEntry(
                        study_date=study_date,
                        task_title=entry["task_title"],
                        subject_name=entry["subject_name"],
                        hours=entry["hours"],
                        deadline=entry["deadline"],
                        plan_id=saved_plan.id,
                    )
                )

        db.session.commit()
        flash("Planner generated and saved.", "success")
        return render_template(
            "planner.html",
            daily_hours=daily_hours,
            plan=plan,
            unscheduled_tasks=unscheduled_tasks,
            saved_plan=saved_plan,
        )

    return render_template("planner_form.html")


@flask_app.route("/saved-plans")
@login_required
def saved_plans():
    plans = (
        StudyPlan.query.filter_by(user_id=current_user.id)
        .order_by(StudyPlan.id.desc())
        .all()
    )
    return render_template("saved_plans.html", plans=plans)


@flask_app.route("/saved-plans/<int:plan_id>")
@login_required
def saved_plan_detail(plan_id):
    saved_plan = StudyPlan.query.filter_by(id=plan_id, user_id=current_user.id).first_or_404()
    plan = group_saved_entries(saved_plan.entries, saved_plan.daily_hours)
    return render_template(
        "planner.html",
        daily_hours=saved_plan.daily_hours,
        plan=plan,
        unscheduled_tasks=[],
        saved_plan=saved_plan,
        viewing_saved_plan=True,
    )


@flask_app.route("/saved-plans/<int:plan_id>/export")
@login_required
def export_saved_plan(plan_id):
    saved_plan = StudyPlan.query.filter_by(id=plan_id, user_id=current_user.id).first_or_404()
    payload = {
        "id": saved_plan.id,
        "title": saved_plan.title,
        "daily_hours": saved_plan.daily_hours,
        "created_at": saved_plan.created_at,
        "entries": [
            {
                "study_date": entry.study_date,
                "task_title": entry.task_title,
                "subject_name": entry.subject_name,
                "hours": entry.hours,
                "deadline": entry.deadline,
            }
            for entry in sorted(saved_plan.entries, key=lambda item: (item.study_date, item.id))
        ],
    }
    response = jsonify(payload)
    response.headers["Content-Disposition"] = f'attachment; filename="study_plan_{saved_plan.id}.json"'
    return response


@flask_app.route("/ai", methods=["GET", "POST"])
@login_required
# Handle the multi-turn TutorAi chat assistant.
def ai():
    response = None
    api_key_configured = bool(os.getenv("GEMINI_API_KEY"))
    chat_turns = get_ai_chat_turns()

    if request.method == "POST":
        action = request.form.get("action", "ask").strip()
        if action == "clear":
            session.pop("ai_chat_turns", None)
            flash("Chat cleared.", "success")
            return redirect(url_for("ai"))

        user_input = request.form["question"].strip()

        try:
            client = get_gemini_client()
            completion = client.models.generate_content(
                model="gemini-2.5-flash",
                config=types.GenerateContentConfig(
                    system_instruction="You are a helpful study assistant."
                ),
                contents=build_chat_prompt(chat_turns, user_input),
            )
            response = clean_ai_response_text(completion.text)
            chat_turns.append({"question": user_input, "answer": response})
            save_ai_chat_turns(chat_turns)

        except Exception as e:
            response = f"Error: {str(e)}"

    return render_template(
        "ai.html",
        response=response,
        api_key_configured=api_key_configured,
        chat_turns=get_ai_chat_turns(),
    )


@flask_app.route("/upload", methods=["GET", "POST"])
@login_required
# Handle uploads for typed PDFs, scanned PDFs, and handwritten image notes.
def upload():
    result = None

    if request.method == "POST":
        uploaded_file = request.files.get("file")

        if not uploaded_file or uploaded_file.filename == "":
            flash("Please choose a PDF file to upload.", "error")
            return render_template("upload.html", result=result)

        filename = secure_filename(uploaded_file.filename)
        extension = Path(filename).suffix.lower()
        supported_mime_types = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }

        if extension not in supported_mime_types:
            flash("Upload a PDF or an image file like PNG, JPG, JPEG, or WEBP.", "error")
            return render_template("upload.html", result=result)

        file_path = Path(flask_app.config["UPLOAD_FOLDER"]) / filename
        uploaded_file.save(file_path)

        try:
            mime_type = supported_mime_types[extension]
            if extension == ".pdf":
                try:
                    extracted_text = extract_pdf_text(file_path)
                except Exception:
                    extracted_text = ""

                if extracted_text.strip():
                    client = get_gemini_client()
                    completion = client.models.generate_content(
                        model="gemini-2.5-flash",
                        config=types.GenerateContentConfig(
                            system_instruction=(
                                "You are a study assistant. Read the student's uploaded notes and return "
                                "three sections only: Summary, Key Points, and Quiz Questions. "
                                "Make the output clear and student-friendly."
                            )
                        ),
                        contents=(
                            "Analyze these study notes.\n\n"
                            "Return:\n"
                            "1. A short summary\n"
                            "2. Bullet key points\n"
                            "3. 5 quiz questions\n\n"
                            f"Notes:\n{extracted_text[:12000]}"
                        ),
                    )
                    result = clean_ai_response_text(completion.text)
                else:
                    result = analyze_notes_with_gemini(file_path, mime_type)
            else:
                result = analyze_notes_with_gemini(file_path, mime_type)

            flash("File uploaded and analyzed successfully.", "success")
        except Exception as e:
            result = f"Error: {str(e)}"

    return render_template("upload.html", result=result)


@flask_app.route("/save-study-session", methods=["POST"])
@login_required
def save_study_session():
    subject_id = request.form.get("subject_id", "").strip()
    minutes_raw = request.form.get("minutes", "").strip()

    try:
        minutes = int(minutes_raw)
    except ValueError:
        return jsonify({"ok": False, "message": "Minutes must be a whole number."}), 400

    if minutes < 1:
        return jsonify({"ok": False, "message": "Minutes must be at least 1."}), 400

    active_session = ActiveStudySession.query.filter_by(user_id=current_user.id).first()
    if active_session:
        subject_id = str(active_session.subject_id)

    subject = Subject.query.filter_by(id=subject_id, user_id=current_user.id).first()
    if not subject:
        return jsonify({"ok": False, "message": "Please choose a valid subject."}), 404

    session = StudySession(
        minutes=minutes,
        studied_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user_id=current_user.id,
        subject_id=subject.id,
    )
    db.session.add(session)
    if active_session:
        db.session.delete(active_session)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "message": f"Saved {minutes} minute(s) for {subject.name}.",
            "subject": subject.name,
            "minutes": minutes,
            "studied_at": session.studied_at,
        }
    )


@flask_app.route("/start-study-session", methods=["POST"])
@login_required
def start_study_session():
    subject_id = request.form.get("subject_id", "").strip()
    subject = Subject.query.filter_by(id=subject_id, user_id=current_user.id).first()
    if not subject:
        return jsonify({"ok": False, "message": "Please choose a valid subject."}), 404

    active_session = ActiveStudySession.query.filter_by(user_id=current_user.id).first()
    if active_session:
        active_session.subject_id = subject.id
        active_session.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        active_session = ActiveStudySession(
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_id=current_user.id,
            subject_id=subject.id,
        )
        db.session.add(active_session)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "message": f"Started study session for {subject.name}.",
            "started_at": active_session.started_at,
            "subject": subject.name,
            "subject_id": subject.id,
        }
    )


@flask_app.route("/active-study-session")
@login_required
def active_study_session():
    active_session = ActiveStudySession.query.filter_by(user_id=current_user.id).first()
    if not active_session:
        return jsonify({"ok": False, "active": False})

    return jsonify(
        {
            "ok": True,
            "active": True,
            "started_at": active_session.started_at,
            "subject": active_session.subject.name,
            "subject_id": active_session.subject_id,
        }
    )


@flask_app.route("/start-pomodoro-session", methods=["POST"])
@login_required
def start_pomodoro_session():
    mode = request.form.get("mode", "focus").strip()
    duration_seconds = 60 * 60 if mode == "focus" else 10 * 60

    active_pomodoro = ActivePomodoroSession.query.filter_by(user_id=current_user.id).first()
    if active_pomodoro:
        active_pomodoro.mode = mode
        active_pomodoro.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        active_pomodoro.duration_seconds = duration_seconds
    else:
        active_pomodoro = ActivePomodoroSession(
            mode=mode,
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=duration_seconds,
            user_id=current_user.id,
        )
        db.session.add(active_pomodoro)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "mode": active_pomodoro.mode,
            "started_at": active_pomodoro.started_at,
            "duration_seconds": active_pomodoro.duration_seconds,
        }
    )


@flask_app.route("/active-pomodoro-session")
@login_required
def active_pomodoro_session():
    active_pomodoro = ActivePomodoroSession.query.filter_by(user_id=current_user.id).first()
    if not active_pomodoro:
        return jsonify({"ok": False, "active": False})

    return jsonify(
        {
            "ok": True,
            "active": True,
            "mode": active_pomodoro.mode,
            "started_at": active_pomodoro.started_at,
            "duration_seconds": active_pomodoro.duration_seconds,
        }
    )


@flask_app.route("/reset-pomodoro-session", methods=["POST"])
@login_required
def reset_pomodoro_session():
    active_pomodoro = ActivePomodoroSession.query.filter_by(user_id=current_user.id).first()
    if active_pomodoro:
        db.session.delete(active_pomodoro)
        db.session.commit()

    return jsonify({"ok": True})


@flask_app.route("/complete-pomodoro-session", methods=["POST"])
@login_required
def complete_pomodoro_session():
    mode = request.form.get("mode", "focus").strip()
    duration_seconds_raw = request.form.get("duration_seconds", "0").strip()

    try:
        duration_seconds = int(duration_seconds_raw)
    except ValueError:
        return jsonify({"ok": False, "message": "Invalid duration."}), 400

    pomodoro_session = PomodoroSession(
        mode=mode,
        completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        duration_seconds=duration_seconds,
        user_id=current_user.id,
    )
    db.session.add(pomodoro_session)

    active_pomodoro = ActivePomodoroSession.query.filter_by(user_id=current_user.id).first()
    if active_pomodoro:
        db.session.delete(active_pomodoro)

    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "message": f"Saved completed {mode} session.",
        }
    )


@flask_app.route("/ai-plan-update", methods=["POST"])
@login_required
def ai_plan_update():
    tasks = (
        Task.query.join(Subject)
        .filter(Subject.user_id == current_user.id)
        .order_by(Task.deadline.asc(), Task.id.asc())
        .all()
    )
    sessions = (
        StudySession.query.join(Subject)
        .filter(StudySession.user_id == current_user.id)
        .all()
    )

    subject_minutes = {}
    for session in sessions:
        subject_minutes[session.subject.name] = subject_minutes.get(session.subject.name, 0) + session.minutes

    try:
        client = get_gemini_client()
        completion = client.models.generate_content(
            model="gemini-2.5-flash",
            config=types.GenerateContentConfig(
                system_instruction="You are a supportive study coach who gives concise daily guidance."
            ),
            contents=build_ai_plan_prompt(tasks, subject_minutes),
        )
        message = completion.text
    except Exception:
        message = build_fallback_ai_update(tasks, subject_minutes)

    return jsonify({"ok": True, "message": message})


app = WSGIMiddleware(flask_app)

if __name__ == "__main__":
    flask_app.run(debug=True, port=8010)
