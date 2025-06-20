This project converts json files from the old Postman Format(legacy v2.1.0) to the latest Postman format v2.2.0.
Use the code from app.py when the POSTMAN JSON has a single endpoint call with multiple requests and folderstructure.py when a JSON with nested structure consisting of different callbacks for different events.
This is so becuase as of now JSON with its schema mentioned as v2.2.0 when used in POSTMAN only supports a simple structure and throws an error when a JSON with nested structure/multiple endpoints are imported.
