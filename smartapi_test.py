from SmartApi import SmartConnect


def main():
    api_key = 'DUMMY_API_KEY'
    username = 'DUMMY001'
    pwd = 'password'
    totp = "123456"
    smart_api = SmartConnect(api_key, root="http://127.0.0.1:8000")
    print(smart_api.generateSession(username, pwd, totp))


if __name__ == "__main__":
    main()
