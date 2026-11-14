"""Project-wide direct networking; never changes Windows or user proxy settings."""
import os


def direct_environment(environment=None):
    result = dict(os.environ if environment is None else environment)
    for key in list(result):
        if key.lower() in {'http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy', 'ddgs_proxy'}:
            result.pop(key)
    result.update(NO_PROXY='*', no_proxy='*', NODE_USE_ENV_PROXY='0')
    return result


def enforce_direct_networking():
    direct = direct_environment()
    for key in list(os.environ):
        if key not in direct:
            del os.environ[key]
    os.environ.update(direct)


BROWSER_ARGS = ['--no-proxy-server']
