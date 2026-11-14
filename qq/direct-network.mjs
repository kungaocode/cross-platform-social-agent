// This process and its Python workers always connect directly.
for (const key of Object.keys(process.env)) {
  if (['http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy', 'ddgs_proxy'].includes(key.toLowerCase())) {
    delete process.env[key];
  }
}
process.env.NO_PROXY = '*';
process.env.no_proxy = '*';
process.env.NODE_USE_ENV_PROXY = '0';
