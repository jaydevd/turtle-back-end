from rest_framework.response import Response
from common.constants import HTTP_ERROR_CODES

def success_response(status_code, message = "", data = {}):
  print("building payload for the success response")
  
  if status_code == HTTP_ERROR_CODES['200_NO_CONTENT']:
    return Response(status=status_code)
  
  payload = {
    'status': status_code,
    'message': message,
    'data': data,
  }

  print("payload built for response, now returning the response.")

  return Response(payload, status=status_code)

def error_response(status_code, message, errors={}):
  
  print("building error response")
  
  payload = {
    'status': status_code,
    'message': message,
    'data': {},
    'errors': errors
  }

  print("error payload built, now sending the response")
  return Response(payload, status=status_code)