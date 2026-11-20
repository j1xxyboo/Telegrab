# How to push this project to GitHub from a Codespace

**English** · [العربية](#العربية)

These steps upload the project to <https://github.com/j1xxyboo/Telegrab> from a GitHub Codespace.

## Why the normal push fails

A Codespace logs you into GitHub automatically with its own account, using the `GITHUB_TOKEN` variable. If that account can't write to `j1xxyboo/Telegrab`, the push fails with:

```text
Permission to j1xxyboo/Telegrab.git denied to <other-account>. (403)
```

When you try `gh auth login`, it also refuses with:

```text
The value of the GITHUB_TOKEN environment variable is being used for authentication.
To have GitHub CLI store credentials instead, first clear the value from the environment.
```

The fix is to clear that token for one terminal, then sign in as `j1xxyboo`.

## Steps

Do **all** of these in the **same terminal window**. `unset` only lasts for that window, and a new terminal gets the old token back.

**1. Go to the project folder**

```bash
cd /workspaces/codespaces-blank/tele
```

**2. Clear the Codespace token**

```bash
unset GITHUB_TOKEN
```

**3. Sign in as `j1xxyboo`**

```bash
gh auth login -h github.com -p https -w
```

It shows a one-time code and opens a browser page. Paste the code there and sign in with the **`j1xxyboo`** account.

**4. Make git use that login for this project**

```bash
git config --local --replace-all credential.https://github.com.helper ""
git config --local --add credential.https://github.com.helper '!gh auth git-credential'
```

Without this, git in a Codespace keeps using the Codespace's own login.

**5. Check you're signed in as the right account**

```bash
gh auth status
```

It should say `Logged in to github.com account j1xxyboo`.

**6. Push**

```bash
git push -u origin main
```

Open <https://github.com/j1xxyboo/Telegrab> to check the files are there.

## Pushing changes later

After editing files:

```bash
cd /workspaces/codespaces-blank/tele
unset GITHUB_TOKEN
git add -A
git commit -m "Describe your change"
git push
```

## If something goes wrong

| Message | What to do |
| --- | --- |
| `Permission ... denied to <other-account>` | You're in a new terminal, or step 4 wasn't done. Repeat steps 2, 4 and 5. |
| `remote origin already exists` | Fine, ignore it. |
| `fatal: not a git repository` | You're in the wrong folder. Do step 1. |
| `rejected ... fetch first` | The repo on GitHub already has commits. Run `git pull --rebase origin main`, then `git push`. |
| `gh auth status` shows the wrong account | Run `gh auth logout`, then repeat from step 2. |

## Never upload these

They're already blocked by `.gitignore`. Don't force-add them:

- `.env` — your Telegram `api_id` / `api_hash`
- `sessions/` — your logged-in Telegram account
- `cache/`, `logs/`

Never paste a GitHub token or password into chats, issues or files.

---

<div dir="rtl" align="right">

## العربية

# طريقة رفع المشروع إلى GitHub من Codespace

هذه الخطوات ترفع المشروع إلى <https://github.com/j1xxyboo/Telegrab> من داخل GitHub Codespace.

### لماذا يفشل الرفع العادي؟

‏Codespace يسجّل دخولك إلى GitHub تلقائيًا بحسابه الخاص عن طريق المتغير `GITHUB_TOKEN`. إذا لم يكن لهذا الحساب صلاحية الكتابة على `j1xxyboo/Telegrab`، يفشل الرفع بهذه الرسالة:

<div dir="ltr" align="left">

```text
Permission to j1xxyboo/Telegrab.git denied to <other-account>. (403)
```

</div>

وعند تجربة `gh auth login` يرفض أيضًا بهذه الرسالة:

<div dir="ltr" align="left">

```text
The value of the GITHUB_TOKEN environment variable is being used for authentication.
To have GitHub CLI store credentials instead, first clear the value from the environment.
```

</div>

الحل: نحذف هذا التوكن من نافذة الطرفية الحالية، ثم نسجّل الدخول بحساب `j1xxyboo`.

### الخطوات

نفّذ **كل** الخطوات في **نفس نافذة الطرفية (Terminal)**. أمر `unset` يعمل في تلك النافذة فقط، وأي نافذة جديدة سيرجع فيها التوكن القديم.

**1. ادخل إلى مجلد المشروع**

<div dir="ltr" align="left">

```bash
cd /workspaces/codespaces-blank/tele
```

</div>

**2. احذف توكن Codespace**

<div dir="ltr" align="left">

```bash
unset GITHUB_TOKEN
```

</div>

**3. سجّل الدخول بحساب `j1xxyboo`**

<div dir="ltr" align="left">

```bash
gh auth login -h github.com -p https -w
```

</div>

سيظهر لك كود لمرة واحدة وتُفتح صفحة في المتصفح. الصق الكود هناك وسجّل الدخول بحساب **`j1xxyboo`**.

**4. اجعل git يستعمل هذا الحساب في هذا المشروع**

<div dir="ltr" align="left">

```bash
git config --local --replace-all credential.https://github.com.helper ""
git config --local --add credential.https://github.com.helper '!gh auth git-credential'
```

</div>

بدون هذه الخطوة، يبقى git داخل Codespace يستعمل حساب Codespace.

**5. تأكد أنك مسجّل الدخول بالحساب الصحيح**

<div dir="ltr" align="left">

```bash
gh auth status
```

</div>

يجب أن تظهر عبارة `Logged in to github.com account j1xxyboo`.

**6. ارفع المشروع**

<div dir="ltr" align="left">

```bash
git push -u origin main
```

</div>

افتح <https://github.com/j1xxyboo/Telegrab> وتأكد أن الملفات ظهرت.

### رفع تعديلات لاحقًا

بعد تعديل الملفات:

<div dir="ltr" align="left">

```bash
cd /workspaces/codespaces-blank/tele
unset GITHUB_TOKEN
git add -A
git commit -m "Describe your change"
git push
```

</div>

### إذا حدثت مشكلة

| الرسالة | الحل |
| --- | --- |
| `Permission ... denied to <other-account>` | أنت في نافذة طرفية جديدة، أو لم تنفّذ الخطوة 4. أعد الخطوات 2 و 4 و 5. |
| `remote origin already exists` | لا مشكلة، تجاهلها. |
| `fatal: not a git repository` | أنت في مجلد خاطئ. نفّذ الخطوة 1. |
| `rejected ... fetch first` | المستودع على GitHub فيه تعديلات سابقة. نفّذ `git pull --rebase origin main` ثم `git push`. |
| `gh auth status` يُظهر حسابًا خاطئًا | نفّذ `gh auth logout` ثم أعد من الخطوة 2. |

### لا ترفع هذه الملفات أبدًا

هي محجوبة مسبقًا في `.gitignore`، فلا تضفها بالقوة:

- ‏`.env` — مفاتيح تيليجرام `api_id` و `api_hash`
- ‏`sessions/` — حساب تيليجرام الخاص بك وهو مسجّل الدخول
- ‏`cache/` و `logs/`

لا تلصق أبدًا توكن GitHub أو كلمة المرور في المحادثات أو الـ issues أو الملفات.

</div>
