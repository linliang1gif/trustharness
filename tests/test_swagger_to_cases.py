from trustharness.runner.swagger_to_cases import generate_cases_from_swagger_data


def test_generate_cases_ignores_malformed_property_schema():
    spec = {
        "swagger": "2.0",
        "info": {"title": "compatibility", "version": "1.0"},
        "paths": {
            "/orders/save": {
                "post": {
                    "tags": ["orders"],
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "schema": {
                                "type": "object",
                                "required": ["orderNo"],
                                "properties": {
                                    "orderNo": {"type": "string"},
                                    "invalidExportedProperty": "string",
                                },
                            },
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }

    cases, meta = generate_cases_from_swagger_data(spec, generate_l2=False)

    assert meta["stats"]["generated"] == 1
    assert cases[0]["body"]["orderNo"] == "test"
    assert "invalidExportedProperty" not in cases[0]["body"]
