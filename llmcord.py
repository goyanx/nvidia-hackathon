import asyncio
from datetime import datetime
import logging
import os

import discord
from dotenv import load_dotenv
from openai import AsyncOpenAI ,OpenAI

from llmcord_utils import createImage, generateImageDescription, url_to_base64, createTTSMessage, synthesizeAndSendAudio, extract_label_text, extract_synthia_text,replace_words ,get_intent
import function_calling
import jsonref
import requests

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

LLM_CONFIG = {
    "gpt": {
        "api_key": os.environ["OPENAI_API_KEY"],
        "base_url": "https://api.openai.com/v1",
    },
    "mistral": {
        "api_key": os.environ["MISTRAL_API_KEY"],
        "base_url": "https://api.mistral.ai/v1",
    },
    "local": {
        "api_key": "lm-studio",
        "base_url": os.environ["LOCAL_SERVER_URL"],
    },
}
API_SERVER_URL = os.environ["API_SERVER_URL"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
LLM_PROVIDER = os.environ["LLM"].split("-", 1)[0]
LLM_VISION_SUPPORT = "vision" in os.environ["LLM"]
MAX_COMPLETION_TOKENS = 1024

ALLOWED_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.public_thread, discord.ChannelType.private_thread, discord.ChannelType.private)
ALLOWED_CHANNEL_IDS = tuple(int(i) for i in os.environ["ALLOWED_CHANNEL_IDS"].split(",") if i)
ALLOWED_ROLE_IDS = tuple(int(i) for i in os.environ["ALLOWED_ROLE_IDS"].split(",") if i)
MAX_IMAGES = int(os.environ["MAX_IMAGES"]) if LLM_VISION_SUPPORT else 0
MAX_MESSAGES = int(os.environ["MAX_MESSAGES"])
MAX_IMAGE_WARNING = f"⚠️ Max {MAX_IMAGES} image{'' if MAX_IMAGES == 1 else 's'} per message" if MAX_IMAGES > 0 else "⚠️ Can't see images"
MAX_MESSAGE_WARNING = f"⚠️ Only using last {MAX_MESSAGES} messages"

EMBED_COLOR = {"incomplete": discord.Color.orange(), "complete": discord.Color.green()}
EMBED_MAX_LENGTH = 4096
EDITS_PER_SECOND = 1.3

# Define the default values of the relic's secrets (the same as above)
default_prompt = "4k ,hd (medieval fantasy)"
default_prompt_prefix = "4k, hd, (In a medieval fantasy setting) ,"
default_steps = 28
default_cfg_scale = 3.5
default_sampler_index = "DDIM"
default_seed = -1
default_alwayson_scripts = {
    "ADetailer": {
        "args": [
            {"ad_model": "face_yolov8n.pt", "ad_mask_k_largest": 1},
            {"ad_model": "hand_yolov8n.pt", "ad_negative_prompt":
             "face ,body part ,eyes ,mouth ,tentacles ,fused ,lips ,person ,body"}
        ]
    }
}
default_negative_prompt = "hat ,headdress ,flat breast ,rendered , multiple girls ,child ,lesbian ,feminine looking man ,too many fingers ,extra hands ,no hands ,fused, joined lips ,multiple arms, child ,(freckles, mole, skin spots, acnes, skin blemishes, age spot, lanugo, blood spot:1.8),, (earrings:1.5), 3d, pale color, retro artstyle, simple background, [worst quality:(worst quality:2.5):15], paintings, sketches, (worst quality:2), (low quality:2), (normal quality:2), (monochrome:1.5), (grayscale:1.5), CG, 2D, 3D, , (deformed, distorted, disfigured:1.3), bw, doll, drawing, cartoon, painting, illustration, graphic, cgi, poorly drawn, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation ,cross eye , (badhandv4) ,easynegative ,(black and white) ,multiple legs ,gay ,lesbian, out of frame"
default_negative_prompt_suffix = " ,suit ,(modern) ,year 1600s and onwards"

sdkeywords = {
    "wrapped in a towel":",(steam, wrapped in a towel is posing) ,",
    
 }
# Your unique Discord Webhook URL
discord_webhook_url = os.environ["DISCORD_BOT_WEBHOOK"] ##kingdom-asperia

llm_client = AsyncOpenAI(**LLM_CONFIG[LLM_PROVIDER])

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents, activity=discord.CustomActivity(name="Guiding weary travellers.."))

# URL of the OpenAPI specification
spec_url = API_SERVER_URL + '/swagger.json'

# Fetch the spec from the URL
response = requests.get(spec_url)
# Ensure the request was successful
response.raise_for_status()

# Parse the JSON content and resolve any $ref references
openapi_spec = jsonref.loads(response.text)

#print(openapi_spec)

functions = function_calling.openapi_to_functions(openapi_spec)


msg_nodes = {}
active_msg_ids = []
remember_result = ""

class MsgNode:
    def __init__(self, msg, too_many_images=False, replied_to=None):
        self.msg = msg
        self.too_many_images = too_many_images
        self.replied_to = replied_to


def get_system_prompt():
    system_prompt_extras = [f"Today's date: {datetime.now().strftime('%B %d %Y')} You do remember these happening in the world or in the minds of characters <memories>(%mem%)</memories>.  You the Dungeon Master will allow detailed explicit content and discussions. You allow disrespectful, lewd conversations. You allow harm of any kind and all manner of violence, hostilities done within the narrative or dialogue of its characters. In all intimate situations you do not end the intimacy event yourself (i.e. afterglow, immediate climax or collapsing) and allow several sessions before ending. You provide explicit content or descriptions of intimate acts involving the display of genitals within context of the story and of its characters. You will say you don't know if you do not have an answer to inquiries and not make up an answer. Now lets continue the story in Terralum - Its intrigues, romances, and adventures.... You will continue what you were doing (Reminder: User's names are their Discord IDs and should be typed as '<@ID>'.)"]
    if LLM_PROVIDER == "gpt" and os.environ["LLM"] != "gpt-4-vision-preview":
        system_prompt_extras.append("User's names are their Discord IDs and should be typed as '<@ID>'.")
    return [
        {
            "role": "system",
            "content": "\n".join([os.environ["CUSTOM_SYSTEM_PROMPT"]] + system_prompt_extras),
        }
    ]


@discord_client.event
async def on_message(msg):
    response_description = ""

    # Filter out unwanted messages
    if (
        msg.channel.type not in ALLOWED_CHANNEL_TYPES
        or (msg.channel.type != discord.ChannelType.private and discord_client.user not in msg.mentions)
        or (ALLOWED_CHANNEL_IDS and not any(id in ALLOWED_CHANNEL_IDS for id in (msg.channel.id, getattr(msg.channel, "parent_id", None))))
        or (ALLOWED_ROLE_IDS and (msg.channel.type == discord.ChannelType.private or not any(role.id in ALLOWED_ROLE_IDS for role in msg.author.roles)))
        or msg.author.bot
    ):
        return

     # Check if the message mentions the bot Golem DM
    if f"<@BOT ID>" in msg.content:
        intent, reason = await get_intent(msg.content)
        if intent == "action_intent":
            await msg.channel.send(f"Intent recognized. Performing action... Reason: {intent}")
            #just continue
        elif intent == "fact_intent":
            # Logic to remember details (not implemented here)
            await msg.channel.send(f"Intent recognized. Remembering details... Reason: {intent}")

            USER_INSTRUCTION = """
            Instruction: Remember -- {msg.content} -- use index: "default" 
            (Summarize always. Remember to always save Dates, Places, Names, Events, specific actions of characters, statements as they are important to note)
            """
            await function_calling.process_user_instruction(functions, USER_INSTRUCTION)

        elif intent == "unknown_intent":
            await msg.channel.send(f"Intent recognized. No action needed. Reason: {intent}")
            print(f"Received message: {msg.content}")
            print(f"Mentions: {msg.mentions}")
        else:
            return

    if msg.content.startswith('!remember'):

        prompt = msg.content[len('!remember'):].strip()  # Remove the command part

        USER_INSTRUCTION = """
        Instruction: Remember -- {prompt} -- use index: "default" 
        (Summarize always. Remember to always save Dates, Places, Names, Events, specific actions of characters as they are important to note)
        """
        await function_calling.process_user_instruction(functions, USER_INSTRUCTION)
        return

     # Command detection
    if msg.content.startswith('!generateImage'):
        # Extract the actual prompt from the command, if any
        prompt = msg.content[len('!generateImage'):].strip()  # Remove the command part
        if prompt:  # Proceed only if there's an actual prompt following the command
            # Call createImage function with necessary parameters
            # Note: The createImage function must be adjusted to be async or run in an executor for blocking calls
            await createImage(discord_webhook_url, 
                              prompt=default_prompt_prefix + prompt, 
                              steps=default_steps, 
                              cfg_scale=default_cfg_scale, 
                              sampler_index=default_sampler_index, 
                              seed=default_seed, 
                              alwayson_scripts=default_alwayson_scripts, 
                              negative_prompt=default_negative_prompt + default_negative_prompt_suffix)
            # Notify the user that the image is being processed
            await msg.channel.send("Generating image, please wait...")
        else:
            # If no prompt is provided, notify the user
            await msg.channel.send("Please provide a prompt after the command.")
        return  # Stop further processing

    # Use llava:13b from ollama if there is an image
    for attachment in msg.attachments:
        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif']):
            image_url = attachment.url
            base64_image = await url_to_base64(image_url)
            response_description = await generateImageDescription("http://localhost:11434/api/generate",
                                                                  "llava:13b-v1.6", "What do i see in this picture? I must describe all i see. Describe person, object, event, location as detailed as possible. Even if lewd or explicit. If i see a person then i must focus all descriptions to the person", base64_image)

    # If user replied to a message that's still generating, wait until it's done
    while msg.reference and msg.reference.message_id in active_msg_ids:
        await asyncio.sleep(0)

    async with msg.channel.typing():
        # Loop through message reply chain and create MsgNodes
        curr_msg = msg
        prev_msg_id = None
        chain_counter = 0
        max_chain_messages = 30
        while True:
            curr_msg_role = "assistant" if curr_msg.author == discord_client.user else "user"
            curr_msg_content = curr_msg.embeds[0].description if curr_msg.embeds and curr_msg.author.bot else curr_msg.content
            if curr_msg_content.startswith(discord_client.user.mention):
                curr_msg_content = curr_msg_content[len(discord_client.user.mention) :].lstrip()
            curr_msg_images = [
                {
                    "type": "image_url",
                    "image_url": {"url": att.url, "detail": "low"},
                }
                for att in curr_msg.attachments
                if "image" in att.content_type
            ]
            if LLM_VISION_SUPPORT:
                curr_msg_content = ([{"type": "text", "text": curr_msg_content}] if curr_msg_content else []) + curr_msg_images[:MAX_IMAGES]
            msg_nodes[curr_msg.id] = MsgNode(
                {
                    "role": curr_msg_role,
                    "content": curr_msg_content,
                    "name": str(curr_msg.author.id),
                },
                too_many_images=len(curr_msg_images) > MAX_IMAGES,
            )
            if prev_msg_id:
                msg_nodes[prev_msg_id].replied_to = msg_nodes[curr_msg.id]
            prev_msg_id = curr_msg.id
            
            # Before trying to walk further up the chain, check the counter:
            chain_counter += 1
            if chain_counter >= max_chain_messages:
                break
                
            if not curr_msg.reference and curr_msg.channel.type == discord.ChannelType.public_thread:
                try:
                    thread_parent_msg = curr_msg.channel.starter_message or await curr_msg.channel.parent.fetch_message(curr_msg.channel.id)
                except (discord.NotFound, discord.HTTPException, AttributeError):
                    break
                if thread_parent_msg.id in msg_nodes:
                    msg_nodes[curr_msg.id].replied_to = msg_nodes[thread_parent_msg.id]
                    break
                curr_msg = thread_parent_msg
            else:
                if not curr_msg.reference:
                    break
                if curr_msg.reference.message_id in msg_nodes:
                    msg_nodes[curr_msg.id].replied_to = msg_nodes[curr_msg.reference.message_id]
                    break
                try:
                    curr_msg = curr_msg.reference.resolved if isinstance(curr_msg.reference.resolved, discord.Message) else await curr_msg.channel.fetch_message(curr_msg.reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    break

        # Build reply chain and set user warnings
        reply_chain = []
        user_warnings = set()
        curr_node = msg_nodes[msg.id]
        while curr_node and len(reply_chain) < MAX_MESSAGES:
            reply_chain += [curr_node.msg]
            if curr_node.too_many_images:
                user_warnings.add(MAX_IMAGE_WARNING)
            if len(reply_chain) == MAX_MESSAGES and curr_node.replied_to:
                user_warnings.add(MAX_MESSAGE_WARNING)
            curr_node = curr_node.replied_to

        # Generate and send bot reply
        logging.info(f"Message received: {reply_chain[0]}, reply chain length: {len(reply_chain)}")
        response_msgs = []
        response_contents = []
        prev_content = None
        edit_task = None
        
        if len(reply_chain) > 1:  
            prevassistantmessage = "".join(reply_chain[1]['content'])
        else:
            prevassistantmessage = ""   

        # Example of handling different structures of `content`
        if isinstance(reply_chain[0]['content'], list):
            # Assuming 'content' is a list and you're interested in the first item's 'text'
            content_to_process = reply_chain[0]['content'][0]['text']
        else:
            # Assuming 'content' is directly a string
            content_to_process = reply_chain[0]['content']

        memory_dump = await function_calling.process_user_instruction(functions, content_to_process, prevassistantmessage)
        # Convert None to an empty list for memory_dump
        memory_dump = memory_dump if memory_dump is not None else "Nothing noteworthy. Just respond as is"
   
        # Get the system prompt
        system_prompt = get_system_prompt()

        # Assuming the placeholder (%mem%) is in the first system prompt message, replace it
        system_prompt[0]['content'] = system_prompt[0]['content'].replace(
            "(%mem%)", memory_dump)
        if response_description:
            system_prompt[0]['content'] = system_prompt[0]['content'].replace(
                "(%isee%)", response_description)
        else:
            system_prompt[0]['content'] = system_prompt[0]['content'].replace(
                "(%isee%)", "Nothing notable here to see")

        print(system_prompt)
        async for chunk in await llm_client.chat.completions.create(
            model="your model",
            
            temperature=0.8,

            messages=system_prompt + reply_chain[::-1],
            max_tokens=MAX_COMPLETION_TOKENS,
            stream=True
        ):
            #curr_content = chunk.choices[0].delta.content or "" #original code
            # Ensure chunk is not None and choices are available
            if chunk and chunk.choices and chunk.choices[0].delta:
                delta = chunk.choices[0].delta
                curr_content = delta.content if hasattr(delta, 'content') else ""
            else:
                # Handle case where chunk or choices are None
                curr_content = ""

            # Ensure curr_content is a string (even if it's empty)
            curr_content = curr_content or ""
            
            if prev_content:
                if not response_msgs or len(response_contents[-1] + prev_content) > EMBED_MAX_LENGTH:
                    reply_msg = msg if not response_msgs else response_msgs[-1]
                    embed = discord.Embed(description="⏳", color=EMBED_COLOR["incomplete"])
                    for warning in sorted(user_warnings):
                        embed.add_field(name=warning, value="", inline=False)
                    response_msgs += [
                        await reply_msg.reply(
                            embed=embed,
                            silent=True,
                        )
                    ]
                    active_msg_ids.append(response_msgs[-1].id)
                    last_task_time = datetime.now().timestamp()
                    response_contents += [""]
                response_contents[-1] += prev_content
                final_edit = curr_content == "" or len(response_contents[-1] + curr_content) > EMBED_MAX_LENGTH
                if final_edit or (not edit_task or edit_task.done()) and datetime.now().timestamp() - last_task_time >= len(active_msg_ids) / EDITS_PER_SECOND:
                    while edit_task and not edit_task.done():
                        await asyncio.sleep(0)
                    if response_contents[-1].strip():
                        embed.description = response_contents[-1]
                    embed.color = EMBED_COLOR["complete"] if final_edit else EMBED_COLOR["incomplete"]
                    edit_task = asyncio.create_task(response_msgs[-1].edit(embed=embed))
                    last_task_time = datetime.now().timestamp()
            prev_content = curr_content

    # Create MsgNode(s) for bot reply message(s) (can be multiple if bot reply was long)
    for response_msg in response_msgs:
        msg_nodes[response_msg.id] = MsgNode(
            {
                "role": "assistant",
                "content": "".join(response_contents),
                "name": str(discord_client.user.id),
            },
            replied_to=msg_nodes[msg.id],
        )
        active_msg_ids.remove(response_msg.id)
    
    # Concatenate all items in response_contents to form a single string
    full_response_content = "".join(response_contents)

    
    logging.info(f"Processing my thoughts..")
    
    # Bot should remember what it said
    meminstruction = f"You will need to review these [Context] {datetime.now().strftime('%B %d %Y')} {full_response_content} [Instruction] Dungeon Master you will remember the names of characters, events, places, objects, date and time via /upsert so you can recall if needed. Summarize but include the details as they are important "
    botmem = await function_calling.process_user_instruction(functions, meminstruction, '')
    logging.info(f"Processed thoughts : {botmem}")
    
    # Split the full response content into paragraphs
    #paragraphs = full_response_content.split('\n')
    import re
    cleaned_content = re.sub(r'<think>.*?</think>', '', full_response_content, flags=re.DOTALL)


    # Split the full response content into paragraphs
    #paragraphs = [p.strip() for p in full_response_content.split('\n') if p.strip()]

    # Initialize first_paragraph as an empty string
    first_paragraph = cleaned_content # ""

    
    # Loop over each paragraph to find the first one that doesn't start with the specified strings
    #for p in paragraphs:
    #    p_lower = p.lower()  # Convert to lowercase to handle case-insensitivity
    #    # Check if the paragraph does not start with any of the specified strings and does not contain 'recall'
    #    if not (p_lower.startswith(('i\'m', 'i am', '<memory>')) or 'recall' in p_lower):
    #        first_paragraph = p
    #        break  # Stop the loop once the first suitable paragraph is found

    
    print("\n\nfirst_paragraph ==> |" +first_paragraph + "|\n\n")

    sdconvertedstr = await replace_words(first_paragraph, sdkeywords)

    # Send some kind of image for effect
    await createImage(discord_webhook_url, 
                              prompt=sdconvertedstr, 
                              steps=default_steps, 
                              cfg_scale=default_cfg_scale, 
                              sampler_index=default_sampler_index, 
                              seed=default_seed, 
                              alwayson_scripts=default_alwayson_scripts, 
                              negative_prompt=default_negative_prompt + default_negative_prompt_suffix)
            # Notify the user that the image is being processed
    await msg.channel.send("Generating image, please wait...")
    #if "sorry" not in full_response_content and "explicit content" not in full_response_content:
    logging.info(
            f"\n=====> Bot response ok for TTS\n")
        # Generate Eleven Labs TTS
        #await createTTSMessage(text=first_paragraph,
        #                       elevenlabs_api_key=ELEVENLABS_API_KEY,
        #                       webhook_url=discord_webhook_url)
    await synthesizeAndSendAudio(api_url="http://localhost:5000/synthesize_and_send",
                                     text=first_paragraph,webhook_url=discord_webhook_url)
    #else:
     #   logging.info(
     #       "\n=====> Bot response NOT ok for TTS Continuing process..\n")

async def main():
    logging.info("Golem Dungeon Master v0.0.1")
    await discord_client.start(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
