# models.py
# This file contains the database models (tables)

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

# Create the SQLAlchemy database object
db = SQLAlchemy()

# User table for authentication
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)          # unique ID for each user
    username = db.Column(db.String(100), unique=True, nullable=False)     # username (must be unique)
    password = db.Column(db.String(200), nullable=False)                  # hashed password

    # Relationship to subjects
    subjects = db.relationship('Subject', backref='user', lazy=True)
    study_plans = db.relationship('StudyPlan', backref='user', lazy=True, cascade='all, delete-orphan')
    study_sessions = db.relationship('StudySession', backref='user', lazy=True, cascade='all, delete-orphan')
    active_study_sessions = db.relationship('ActiveStudySession', backref='user', lazy=True, cascade='all, delete-orphan')
    active_pomodoro_sessions = db.relationship('ActivePomodoroSession', backref='user', lazy=True, cascade='all, delete-orphan')
    pomodoro_sessions = db.relationship('PomodoroSession', backref='user', lazy=True, cascade='all, delete-orphan')

# Subject table to store subjects for each user
class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)                       # subject name
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)  # link to user

    # Relationship to tasks
    tasks = db.relationship('Task', backref='subject', lazy=True)
    study_sessions = db.relationship('StudySession', backref='subject', lazy=True, cascade='all, delete-orphan')
    active_study_sessions = db.relationship('ActiveStudySession', backref='subject', lazy=True, cascade='all, delete-orphan')

# Task table to store tasks for subjects
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)                      # task title
    deadline = db.Column(db.String(100))                  # deadline as string for simplicity
    estimated_hours_per_task = db.Column(db.Integer, nullable=False, default=1)
    is_completed = db.Column(db.Boolean, nullable=False, default=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)  # link to subject


class StudyPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    daily_hours = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    entries = db.relationship('StudyPlanEntry', backref='plan', lazy=True, cascade='all, delete-orphan')


class StudyPlanEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    study_date = db.Column(db.String(100), nullable=False)
    task_title = db.Column(db.String(200), nullable=False)
    subject_name = db.Column(db.String(100), nullable=False)
    hours = db.Column(db.Integer, nullable=False)
    deadline = db.Column(db.String(100), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey('study_plan.id'), nullable=False)


class StudySession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    minutes = db.Column(db.Integer, nullable=False)
    studied_at = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)


class ActiveStudySession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)


class ActivePomodoroSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(20), nullable=False)
    started_at = db.Column(db.String(100), nullable=False)
    duration_seconds = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)


class PomodoroSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mode = db.Column(db.String(20), nullable=False)
    completed_at = db.Column(db.String(100), nullable=False)
    duration_seconds = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
