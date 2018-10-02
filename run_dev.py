from funding.factory import create_app
import settings

if __name__ == '__main__':
    app = create_app()
    app.run(host=settings.BIND_HOST, port=settings.BIND_PORT,
            debug=settings.DEBUG, use_reloader=False)
