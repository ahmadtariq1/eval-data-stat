import csv
import json
import os
import random
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import streamlit as st

DATA_PATH = "questions_only.jsonl"
DB_PATH = "evaluation_stats.csv"

ROLES = ["Premed Student", "Med Student", "Teacher", "Doctor", "Other"]

CSV_HEADERS = [
    "Timestamp",
    "Reviewer_Email",
    "Reviewer_Role",
    "Question_ID",
    "Is_Correct",
    "Difficulty",
    "MDCAT_Alignment",
    "Comments",
]


@dataclass(frozen=True)
class Question:
    uid: str
    batch_idx: int
    question_number: int
    question_text: str
    options: Dict[str, str]
    correct_answer: str


def _normalize_options(options: Any) -> Dict[str, str]:
    if not isinstance(options, dict):
        return {}
    normalized: Dict[str, str] = {}
    for k, v in options.items():
        if k is None:
            continue
        normalized[str(k).strip().upper()] = "" if v is None else str(v)
    return normalized


def ensure_csv_exists(path: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)


def load_evaluations_for_user_csv(path: str, reviewer_email: str) -> Set[str]:
    seen: Set[str] = set()
    if not os.path.exists(path):
        return seen

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("Reviewer_Email") or "").strip() == reviewer_email.strip():
                qid = (row.get("Question_ID") or "").strip()
                if qid:
                    seen.add(qid)
    return seen


def append_rows_to_csv(path: str, rows: List[List[str]]) -> None:
    ensure_csv_exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def _gsheets_config_from_secrets() -> Optional[Tuple[dict, str]]:
    """Reads Streamlit secrets.

    Supports the exact structure the user created:
      - [gcp_service_account] ...service account fields...
      - GSHEET_ID (string)
    """
    try:
        sa = st.secrets.get("gcp_service_account")
        sheet_id = st.secrets.get("GSHEET_ID")
    except Exception:
        return None

    if not sa or not sheet_id:
        return None

    if not isinstance(sa, dict):
        return None

    return sa, str(sheet_id)


def _log_exception(prefix: str, exc: BaseException) -> None:
    # Streamlit Cloud shows stdout/stderr in logs; also show in-app for visibility.
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(prefix)
    print(detail)
    st.error(prefix)
    with st.expander("Details (exception)"):
        st.code(detail)


@st.cache_resource(show_spinner=False)
def _get_gspread_client(service_account_info: dict):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    return gspread.authorize(creds)


def _open_worksheet(service_account_info: dict, spreadsheet_id: str):
    client = _get_gspread_client(service_account_info)
    sh = client.open_by_key(spreadsheet_id)
    # Use the first worksheet by default.
    return sh.sheet1


def _can_use_sheets(cfg: Tuple[dict, str]) -> bool:
    """Return True only if we can actually open the spreadsheet/worksheet."""
    try:
        sa_info, sheet_id = cfg
        ws = _open_worksheet(sa_info, sheet_id)
        # Touch an attribute to ensure object is valid.
        _ = ws.title
        return True
    except Exception as e:
        _log_exception("Google Sheets preflight check failed (cannot open spreadsheet).", e)
        return False


def ensure_sheet_headers(ws) -> None:
    expected = CSV_HEADERS
    try:
        first_row = ws.row_values(1)
    except Exception:
        first_row = []

    if [c.strip() for c in first_row] == expected:
        return

    if not first_row:
        ws.append_row(expected, value_input_option="RAW")
        return

    st.warning(
        "Google Sheet header row doesn't match expected columns. "
        "Expected: " + ", ".join(expected)
    )


def load_evaluations_for_user_sheet(ws, reviewer_email: str) -> Set[str]:
    seen: Set[str] = set()
    try:
        records = ws.get_all_records()  # uses first row as header
    except Exception as e:
        st.error(f"Failed to read Google Sheet records: {e}")
        return seen

    target = reviewer_email.strip()
    for r in records:
        if str(r.get("Reviewer_Email", "")).strip() == target:
            qid = str(r.get("Question_ID", "")).strip()
            if qid:
                seen.add(qid)
    return seen


def append_rows_to_sheet(ws, rows: List[List[str]]) -> None:
    ws.append_rows(rows, value_input_option="RAW")


def load_questions(jsonl_path: str) -> List[Question]:
    questions: List[Question] = []

    if not os.path.exists(jsonl_path):
        return questions

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for batch_idx, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            batch_questions = obj.get("questions") or []
            if not isinstance(batch_questions, list):
                continue

            for q in batch_questions:
                if not isinstance(q, dict):
                    continue
                qnum = q.get("question_number")
                try:
                    qnum_int = int(qnum)
                except Exception:
                    continue

                uid = f"Batch{batch_idx}_Q{qnum_int}"
                options = _normalize_options(q.get("options"))
                correct = "" if q.get("correct_answer") is None else str(q.get("correct_answer")).strip().upper()

                questions.append(
                    Question(
                        uid=uid,
                        batch_idx=batch_idx,
                        question_number=qnum_int,
                        question_text="" if q.get("question_text") is None else str(q.get("question_text")),
                        options=options,
                        correct_answer=correct,
                    )
                )

    return questions


def pick_session_batch(unseen: List[Question], n: int = 15) -> List[Question]:
    if len(unseen) <= n:
        return list(unseen)
    return random.sample(unseen, n)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    st.set_page_config(page_title="MDCAT Biology Question Evaluator", layout="wide")
    st.title("MDCAT Biology Question Evaluator")

    gs_cfg = _gsheets_config_from_secrets()
    using_sheets = bool(gs_cfg) and _can_use_sheets(gs_cfg)  # only True if we can open it

    with st.expander("Google Sheets status / debug", expanded=False):
        st.write(
            {
                "has_gcp_service_account_secret": bool(st.secrets.get("gcp_service_account", None)),
                "has_GSHEET_ID_secret": bool(st.secrets.get("GSHEET_ID", None)),
                "using_sheets": using_sheets,
            }
        )

    st.write(
        "Evaluate AI-generated questions. "
        + (
            "Your responses are saved to **Google Sheets** (persistent) and also to a local CSV as a best-effort backup. "
            if using_sheets
            else "Your responses are saved locally to `evaluation_stats.csv` (not persistent on Streamlit Cloud). "
        )
        + "You won't see the same question twice under the same Email/Name."
    )

    # --- Onboarding
    reviewer_email = st.text_input("Your Email or Name", key="reviewer_email")
    reviewer_role = st.selectbox("Your Role", options=ROLES, index=None, placeholder="Select your role", key="reviewer_role")

    if not reviewer_email or not reviewer_role:
        st.info("Please enter your Email/Name and select your role to begin.")
        return

    # --- Load data
    all_questions = load_questions(DATA_PATH)
    if not all_questions:
        st.error(f"No questions found. Make sure `{DATA_PATH}` exists and is valid JSONL.")
        return

    # --- Load seen IDs (Sheets first; fallback to local CSV)
    seen_ids: Set[str] = set()

    ws = None
    if using_sheets:
        try:
            sa_info, sheet_id = gs_cfg  # type: ignore[misc]
            ws = _open_worksheet(sa_info, sheet_id)
            ensure_sheet_headers(ws)
            seen_ids = load_evaluations_for_user_sheet(ws, reviewer_email)

            with st.expander("Google Sheets connection info", expanded=False):
                try:
                    st.write(
                        {
                            "spreadsheet_id": sheet_id,
                            "worksheet_title": getattr(ws, "title", None),
                            "worksheet_id": getattr(ws, "id", None),
                        }
                    )
                except Exception:
                    st.write("Connected, but could not read worksheet metadata.")
        except Exception as e:
            _log_exception("Google Sheets is configured but could not be opened.", e)
            st.info("Falling back to local CSV for this session.")
            using_sheets = False

    if not using_sheets:
        ensure_csv_exists(DB_PATH)
        seen_ids = load_evaluations_for_user_csv(DB_PATH, reviewer_email)

    unseen = [q for q in all_questions if q.uid not in seen_ids]

    st.caption(f"Total questions: {len(all_questions)} | Already evaluated by you: {len(seen_ids)} | Remaining: {len(unseen)}")

    if not unseen:
        st.balloons()
        st.success("You're done! You have evaluated all available questions in the dataset.")
        return

    # --- Session batch
    batch_key = "current_batch_uids"
    if batch_key not in st.session_state:
        picked = pick_session_batch(unseen, n=15)
        st.session_state[batch_key] = [q.uid for q in picked]

    # If user evaluated some in another tab, refresh unseen list + batch pruning.
    current_uids: List[str] = list(st.session_state.get(batch_key, []))
    unseen_by_uid = {q.uid: q for q in unseen}
    current_questions: List[Question] = [unseen_by_uid[uid] for uid in current_uids if uid in unseen_by_uid]

    # If batch became empty (e.g., all got evaluated), re-pick.
    if not current_questions:
        picked = pick_session_batch(unseen, n=15)
        st.session_state[batch_key] = [q.uid for q in picked]
        current_questions = picked

    st.subheader("Your current batch")
    st.write(f"Questions in this batch: {len(current_questions)}")

    with st.form("evaluation_form"):
        for i, q in enumerate(current_questions, start=1):
            st.markdown(f"---\n### Q{i}. ({q.uid})\n\n{q.question_text}")

            # Options
            for opt_key in ["A", "B", "C", "D"]:
                if opt_key in q.options:
                    st.markdown(f"- **{opt_key}.** {q.options[opt_key]}")

            st.markdown(f"**Provided correct answer:** `{q.correct_answer}`")

            st.radio(
                "Is the answer correct?",
                options=["Yes", "No", "Needs slight modification"],
                key=f"is_correct__{q.uid}",
                horizontal=True,
            )
            st.radio(
                "Difficulty",
                options=["Easy", "Medium", "Hard"],
                key=f"difficulty__{q.uid}",
                horizontal=True,
            )
            st.radio(
                "Aligns with MDCAT style?",
                options=["Yes", "No"],
                key=f"alignment__{q.uid}",
                horizontal=True,
            )
            st.text_input(
                "Comments / Suggested Fixes (optional)",
                key=f"comments__{q.uid}",
            )

        submitted = st.form_submit_button("Submit Evaluations")

    if not submitted:
        return

    # --- Persist
    rows: List[List[str]] = []
    for q in current_questions:
        is_correct = st.session_state.get(f"is_correct__{q.uid}")
        difficulty = st.session_state.get(f"difficulty__{q.uid}")
        alignment = st.session_state.get(f"alignment__{q.uid}")
        comments = st.session_state.get(f"comments__{q.uid}", "")

        rows.append(
            [
                utc_now_iso(),
                reviewer_email.strip(),
                reviewer_role,
                q.uid,
                str(is_correct or ""),
                str(difficulty or ""),
                str(alignment or ""),
                str(comments or ""),
            ]
        )

    # --- Persist (Sheets + local CSV backup)
    sheets_ok = False
    if using_sheets and ws is not None:
        with st.spinner("Saving to Google Sheets..."):
            try:
                append_rows_to_sheet(ws, rows)
                sheets_ok = True
                st.info(f"Appended {len(rows)} row(s) to Google Sheets.")
            except Exception as e:
                _log_exception("Failed to write to Google Sheets.", e)
                st.info("Your responses will still be written to local CSV (may be ephemeral on Streamlit Cloud).")

    try:
        append_rows_to_csv(DB_PATH, rows)
    except Exception as e:
        st.warning(f"Failed to write local CSV backup: {e}")

    if sheets_ok:
        st.success("Saved to Google Sheets.")
        st.session_state.pop(batch_key, None)

        with st.expander("Submission debug", expanded=False):
            st.write(
                {
                    "using_sheets": using_sheets,
                    "sheets_ok": sheets_ok,
                    "rows": len(rows),
                    "csv_backup": DB_PATH,
                }
            )

        time.sleep(1.5)
        st.rerun()
    else:
        st.warning("Saved locally only. Google Sheets connection FAILED (or not used).")
        st.error(
            "Scroll up and open the error box that says 'Google Sheets preflight check failed'. "
            "Expand the exception details to see exactly why Google rejected the connection."
        )

        with st.expander("Submission debug", expanded=True):
            st.write(
                {
                    "using_sheets": using_sheets,
                    "sheets_ok": sheets_ok,
                    "rows": len(rows),
                    "csv_backup": DB_PATH,
                }
            )

        # Clear batch so the reviewer doesn't get stuck on the same items.
        st.session_state.pop(batch_key, None)
        # Intentionally DO NOT rerun here, so the user can read/debug.


if __name__ == "__main__":
    main()
