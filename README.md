# BRAIN daily bot

Runs every day at 6 pm IST on GitHub's servers. It tops up to 250 simulations
for the day and submits the best passing alphas (2/day until Nov 21, then 1/day),
counting whatever already ran or was submitted earlier that day.

## Setup (5 minutes)
1. Create a **private** repository on github.com (New repository → Private).
2. Upload `brain_daily.py`, `README.md` and the `.github/workflows/brain.yml` file
   (keep that folder path).
3. Settings → Secrets and variables → Actions → New repository secret:
   - `BRAIN_EMAIL` = your BRAIN email
   - `BRAIN_PASSWORD` = your BRAIN password
4. Actions tab → "BRAIN daily" → Run workflow, to test it once.

If the log says BRAIN wants biometric verification, open the link it prints on
your phone, complete it, and run the workflow again.
