# tools.py

FAN_STATE = "off"
LIGHT_STATE = "off"


# -----------------------------------------
# FAN TOOL
# -----------------------------------------

CONTROL_FAN_TOOL = {
    "type": "function",
    "name": "control_fan",
    "description": (
        "Turn the fan on or off. "
        "Use this tool when the user asks to start, stop, "
        "switch on, or switch off the fan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "The required fan state."
            }
        },
        "required": ["state"]
    }
}


def control_fan(state):

    global FAN_STATE

    if state == "on":
        FAN_STATE = "on"

        return {
            "success": True,
            "device": "fan",
            "state": "on",
            "message": "Fan turned ON."
        }

    elif state == "off":
        FAN_STATE = "off"

        return {
            "success": True,
            "device": "fan",
            "state": "off",
            "message": "Fan turned OFF."
        }

    else:
        return {
            "success": False,
            "message": "Invalid fan state."
        }


# -----------------------------------------
# LIGHT TOOL
# -----------------------------------------

CONTROL_LIGHT_TOOL = {
    "type": "function",
    "name": "control_light",
    "description": (
        "Turn the light on or off. "
        "Use this tool when the user asks to turn on, "
        "turn off, switch on, or switch off the light."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "The required light state."
            }
        },
        "required": ["state"]
    }
}


def control_light(state):

    global LIGHT_STATE

    if state == "on":
        LIGHT_STATE = "on"

        return {
            "success": True,
            "device": "light",
            "state": "on",
            "message": "Light turned ON."
        }

    elif state == "off":
        LIGHT_STATE = "off"

        return {
            "success": True,
            "device": "light",
            "state": "off",
            "message": "Light turned OFF."
        }

    else:
        return {
            "success": False,
            "message": "Invalid light state."
        }


# -----------------------------------------
# TOOL DISPATCHER
# -----------------------------------------

def dispatch_tool(name, arguments):

    if name == "control_fan":
        return control_fan(
            arguments.get("state")
        )

    if name == "control_light":
        return control_light(
            arguments.get("state")
        )

    return {
        "success": False,
        "message": f"Unknown tool: {name}"
    }


# -----------------------------------------
# ALL TOOLS
# -----------------------------------------

TOOLS = [
    CONTROL_FAN_TOOL,
    CONTROL_LIGHT_TOOL
]