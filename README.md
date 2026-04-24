# MDCAT Biology Question Evaluator (Streamlit)

This app lets reviewers evaluate AI-generated MDCAT Biology MCQs from `questions_only.jsonl`.

## What it does

- Loads `questions_only.jsonl` (JSON Lines)
- Normalizes options keys to `A/B/C/D` and `correct_answer` to uppercase
- Generates a stable unique question id like `Batch3_Q5`
- Stores evaluations in `evaluation_stats.csv`
- Ensures a reviewer never sees the same question twice (by Email/Name)
- Shows exactly 15 unseen questions per batch (or fewer if less remain)

## Run

```bash
python -m pip install -r requirements.txt
streamlit run app.py
```

## Data format

Each line in `questions_only.jsonl` is a JSON object:

```json
{
  "questions": [
    {
      "question_number": 1,
      "question_text": "...",
      "options": {"A": "...", "B": "...", "C": "...", "D": "..."},
      "correct_answer": "A"
    }
  ],
  "batch_explanation": "..."
}
```

## Output CSV

`evaluation_stats.csv` columns:

- Timestamp (UTC ISO)
- Reviewer_Email
- Reviewer_Role
- Question_ID
- Is_Correct
- Difficulty
- MDCAT_Alignment
- Comments
