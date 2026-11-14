# Optional Douyin backend

The API transport integrates with [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider)
through an isolated Python worker. Its source, virtual environment, account
material and downloaded wheels are not distributed in this repository.

The adapter checks commit `d151f9c2980abc304c1ae4918c2272675cd9e4c2`.
Review the upstream project's terms before obtaining or using it. This project
does not grant a license to that upstream code.

```powershell
git clone https://github.com/cv-cat/DouYin_Spider.git third_party/DouYin_Spider
git -C third_party/DouYin_Spider checkout d151f9c2980abc304c1ae4918c2272675cd9e4c2
python -m venv third_party/DouYin_Spider/.venv
third_party/DouYin_Spider/.venv/Scripts/python.exe -m pip install -r douyin/backend-requirements.lock.txt
```

This is optional. WeChat, QQ, Bilibili and the Douyin browser transport do not
require this backend environment.
