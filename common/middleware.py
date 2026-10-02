class OptionalApiTrailingSlash:
  """
  Treats the trailing slash on `/api/` paths as optional instead of redirecting.

  Django's URLconf is slash-terminated and `APPEND_SLASH` is on, so a request
  for `/api/user/auth/sign-up` gets a 301 pointing at the slashed form. That is
  harmless for a browser navigating to an HTML page, but this API is reached
  through a Next.js proxy that drops the trailing slash before forwarding, so
  every auth call bounced between the two spellings:

      Next.js  308  /api/user/auth/sign-up/ -> /api/user/auth/sign-up
      Django   301  /api/user/auth/sign-up  -> /api/user/auth/sign-up/

  A POST cannot survive that. Django only issues the 301, and a 301 makes the
  browser replay the request as a GET with no body, so the auth payload was
  lost even once the loop was cut short.

  Normalising the path to the slashed form that the URLconf expects means the
  view matches on the first hop, with no redirect, from either a direct client
  or any proxy. Only `/api/` is touched, so `admin/` keeps its normal
  `APPEND_SLASH` behaviour.
  """

  def __init__(self, get_response):
    self.get_response = get_response

  def __call__(self, request):
    path_info = request.path_info

    if path_info.startswith('/api/') and not path_info.endswith('/'):
      request.path_info = f'{path_info}/'
      request.META['PATH_INFO'] = request.path_info

    return self.get_response(request)