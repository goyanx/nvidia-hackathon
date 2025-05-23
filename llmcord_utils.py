import io
import aiohttp
import discord
import base64
from aiohttp import ClientTimeout
import logging
import re

# Configure basic logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')


async def createImage(webhook_url, prompt, steps, cfg_scale, sampler_index, seed, alwayson_scripts, negative_prompt):
    payload = {
        "prompt": prompt,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler_index": sampler_index,
        "seed": seed,
        "alwayson_scripts": alwayson_scripts,
        "negative_prompt": negative_prompt
    }
    headers = {'Content-Type': 'application/json'}

    # Define a 4-second total operation timeout
    timeout = ClientTimeout(total=120)

    # Using aiohttp for async HTTP requests
    async with aiohttp.ClientSession() as session:
        async with session.post('http://localhost:7860/sdapi/v1/txt2img', json=payload, headers=headers, timeout=timeout) as response:
            if response.status == 200:
                image_data = await response.json()
                # Assuming this is how images are returned
                images_base64 = image_data.get('images')

                if images_base64:
                    for i, image_base64 in enumerate(images_base64):
                        image_bytes = base64.b64decode(image_base64)
                        # Convert the bytes into a file-like object
                        image_file = io.BytesIO(image_bytes)
                        image_file.name = f"image_{i+1}.png"

                        # Initialize the webhook with aiohttp session
                        webhook = discord.Webhook.from_url(
                            webhook_url, session=session)
                        await webhook.send(username=f"Dungeon Master Golem - Image {i+1}", files=[discord.File(fp=image_file, filename=f"image_{i+1}.png")])

                    return "The images have been sent to Discord."
                else:
                    return "The Dungeon Master conjured no images."
            else:
                return f"The Dungeon Master could not be reached. Status code: {response.status}"

# Function to get the intent using Ollama model
async def get_intent(message_content):
    url = "http://localhost:11434/api/generate"
    headers = {
        "Content-Type": "application/json"
    }
    data = {
        "model": "dolphin-phi:latest",
        "prompt": f"Classify the following message into one of the following intents: `action_intent` if it requires an action, `fact_intent` if it involves remembering details, and `unknown_intent` if it does not fit either category. Answer only with either action_intent, fact_intent, or unknown_intent. No need to clarify. If message is blank then it is unknown\n\nMessage: \"{message_content}\"\n\nIntent:",
        "stream": False
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            if response.status == 200:
                result = await response.json()
                print(f"\n\n [get_intent RESULT] >> {result} \n\n")
                intent = result.get("response", "").strip()
                reason = result.get("done_reason", "").strip()
                return intent, reason
            else:
                print(f"Error: {response.status}")
                return None, None
            
async def generateImageDescription(api_url, model, prompt, base64_image):
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "images": [base64_image]
    }
    headers = {'Content-Type': 'application/json'}

    # Define a timeout for the operation
    timeout = ClientTimeout(total=100)

    # Using aiohttp for async HTTP requests
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers, timeout=timeout) as response:
            if response.status == 200:
                # Assuming the API returns a JSON response
                response_data = await response.json()
                # Extract the "response" field from the JSON data
                image_description = response_data.get(
                    "response", "No description available.")
                return image_description
            else:
                return f"Failed to generate image description. Status code: {response.status}"


async def url_to_base64(image_url):
    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as response:
            image_bytes = await response.read()
            return base64.b64encode(image_bytes).decode('utf-8')


async def createTTSMessage(webhook_url, text, elevenlabs_api_key):
    try:
        # ElevenLabs TTS API payload
        """payload = {
            "text": text,
            "accent": "american",
            "accent_strength": 0.3,
            "age": "middle_aged",
            "gender": "female"
        }"""
        payload = {
            "text":text,
            
        }
        headers = {
            'Content-Type': 'application/json',
            'xi-api-key': f'{elevenlabs_api_key}'
        }

        # Define a timeout for the operation
        timeout = ClientTimeout(total=100)

        # Using aiohttp for async HTTP requests
        async with aiohttp.ClientSession() as session:
            # Replace the URL with the actual ElevenLabs TTS endpoint
            async with session.post('https://api.elevenlabs.io/v1/text-to-speech/KEY', json=payload, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    audio_data = await response.read()  # Read the response as bytes

                    # Convert the bytes into a file-like object for Discord
                    audio_file = io.BytesIO(audio_data)
                    audio_file.name = "speech.wav"

                    # Initialize the webhook with aiohttp session
                    webhook = discord.Webhook.from_url(
                        webhook_url, session=session)
                    await webhook.send(username="Synth Bot", files=[discord.File(fp=audio_file, filename="speech.wav")])

                    logging.info("The TTS message has been sent to Discord.")
                    return "The TTS message has been sent to Discord."
                else:
                    logging.error(
                        f"Failed to reach TTS service. Status code: {response.status}")
                    return f"Failed to reach TTS service. Status code: {response.status}"
    except Exception as e:
        logging.exception(f"An error occurred: {e}")
        return f"An error occurred: {e}"


async def synthesizeAndSendAudio(api_url, text, webhook_url):
    payload = {
        "text": text,
        "webhook_url": webhook_url
    }
    headers = {'Content-Type': 'application/json'}

    # Define a timeout for the operation
    timeout = ClientTimeout(total=240)

    # Using aiohttp for async HTTP requests
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers, timeout=timeout) as response:
            if response.status == 200:
                # Assuming the API returns a simple JSON response
                response_data = await response.json()
                # Extract the message field from the JSON data
                message = response_data.get("message", "No message returned.")
                return message
            else:
                return f"Failed to synthesize and send audio. Status code: {response.status}"

async def extract_label_text(text, labels, regex_pattern, skip_pattern=None):
    """
    Extracts text following specified labels up to the first blank line, skipping unwanted patterns.

    :param text: str, the input text from which to extract content
    :param labels: list of str, the labels after which to extract the content
    :param regex_pattern: str, the regex pattern to match the content following the label
    :param skip_pattern: str, optional pattern to skip unwanted initial matches
    :return: str, extracted content or a default message if no content is found
    """
    # Combine the labels into a single regex pattern
    combined_labels = '|'.join([re.escape(label) for label in labels])
    
    # Construct the full regex pattern dynamically based on the combined labels and provided pattern
    if skip_pattern:
        full_pattern = rf"({combined_labels})\s*(?:{skip_pattern}){regex_pattern}"
    else:
        full_pattern = rf"({combined_labels})\s*{regex_pattern}"
    
    match = re.search(full_pattern, text, re.DOTALL)
    
    if match:
        return match.group(0).strip()
    return ""
"""    
async def extract_synthia_text(text):
    ""-"
    Extracts text starting with 'Synthia:' up to the first newline.

    :param text: str, the input text from which to extract content
    :return: str, extracted content or a default message if no content is found
    ""-"
    # Define the regex pattern to capture the content after "Synthia:" up to the first newline
    pattern = r"(Synthia:.*?)(?=\n)"
    match = re.search(pattern, text, re.DOTALL)
    
    if match:
        return match.group(1).strip()
    return ""
"""
async def extract_synthia_text(text):
    """
    Extracts text starting with 'Synthia:' up to the first newline,
    and includes the next paragraph.

    :param text: str, the input text from which to extract content
    :return: str, extracted content or a default message if no content is found
    """
    # Define the regex pattern to capture the content after "Synthia:" up to the first newline
    pattern = r"(Synthia:.*?)(?=\n)"
    match = re.search(pattern, text, re.DOTALL)
    
    if match:
        synthia_text = match.group(1).strip()
        synthia_text_end = match.end()
        remaining_text = text[synthia_text_end:].strip()
        
        # Find the next paragraph
        next_paragraph_pattern = r'([^\n]+(?:\n(?!\n)[^\n]*)*)'
        next_match = re.search(next_paragraph_pattern, remaining_text, re.DOTALL)
        
        if next_match:
            return synthia_text + "\n\n" + next_match.group(1).strip()
        
    return ""


async def replace_words(original_string: str, replacements: dict) -> str:
    """
    original = "hello world this is a test"
    replacements = {
        "hello world": "howdy",
        "test": "experiment"
    }
    result = await replace_words(original, replacements)
    print(result)
    """
    # Sort replacements by length of the key in descending order to handle multi-word replacements
    sorted_replacements = dict(sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True))
    
    # Create a pattern to match all keys in the replacements dictionary
    pattern = re.compile('|'.join(re.escape(key) for key in sorted_replacements.keys()))
    
    # Define a function to be used for replacement
    def replace(match):
        return sorted_replacements[match.group(0)]
    
    # Use re.sub with the pattern and replacement function with count=1 to replace only the first occurrence
    new_string = pattern.sub(replace, original_string, count=1)
    
    return new_string

import asyncio
async def main():
    text = """
    
    sfsdfsadf
    asdfasdfasdfsadfasdasd
    sadfsdaf
    asdfasdf

"""
    
    result = await extract_synthia_text(text)
    print(result)  # Output should be the Synthia paragraph and the next string

# Run the main function
#asyncio.run(main())