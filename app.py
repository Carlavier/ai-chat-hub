import os
import streamlit as st
import redis
from openai import OpenAI
from dotenv import load_dotenv
import json
import random
from datetime import datetime
import time

from bot_profiles import BOT_PROFILES, multibot_prompt
from constants import BOT_ARENA_HISTORY_KEY, BOT_ARENA_MAX_MSGS, MULTIBOT_ANSWER_TEMPERATURE, MULTIBOT_HISTORY_COUNT, MULTIBOT_HISTORY_KEY, MULTIBOT_MAX_TURNS, USER_EXPIRE_SECONDS


# Initialize Redis connection (without singleton)


def init_redis():
    if st.secrets["ENV"] == "production":
        return redis.Redis(
            host=st.secrets["REDIS_PUBLIC_ENDPOINT"],
            port=st.secrets["REDIS_PORT"],
            username=st.secrets["REDIS_USERNAME"],
            password=st.secrets["REDIS_PASSWORD"],
            decode_responses=True
        )
    else:
        return redis.Redis(
            host="localhost",
            port=6379,
            decode_responses=True
        )


try:
    redis_client = init_redis()
    redis_client.ping()  # Test connection
except Exception as e:
    st.error(f"Redis connection error: {str(e)}")
    st.stop()

# Initialize OpenAI client
ai_client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

# --- Helper Functions ---


def send_user_message(sender, message):
    msg_data = {
        "sender": sender,
        "text": message,
        "is_bot": False,
        "timestamp": str(datetime.now())
    }
    redis_client.setex(f"user:{sender}", USER_EXPIRE_SECONDS, "active")
    redis_client.rpush("chat_history", json.dumps(msg_data))
    redis_client.ltrim("chat_history", -100, -1)  # Keep only last 100 messages


def get_active_users():
    return [key.split(":")[1] for key in redis_client.keys("user:*")]


def get_chat_history():
    history = redis_client.lrange("chat_history", 0, -1)
    return [json.loads(item) for item in history if item]

# --- Streamlit App ---


def main():
    st.title("💬 Real-Time Chat Hub")

    # Initialize session state
    if "user" not in st.session_state:
        st.session_state.user = f"User_{random.randint(1000, 9999)}"

    tab1, tab2, tab3 = st.tabs(["Real-Time User Chat", "Bot Arena", "User & Multi-Bot Chat"])

    # --- Tab 1: Real-Time User Chat ---
    with tab1:
        # Sidebar controls
        with st.sidebar:
            st.session_state.user = st.text_input(
                "Your Name", st.session_state.user)
            st.markdown("### 👥 Active Users")
            active_users = get_active_users()
            st.write(", ".join(active_users) or "No active users")

        # Message input
        user_input = st.chat_input("Type your message...")
        if user_input:
            send_user_message(st.session_state.user, user_input)
            st.rerun()

        # Display chat history
        chat_history = get_chat_history()
        for msg in reversed(chat_history[-20:]):  # Show last 20 messages
            with st.chat_message(msg["sender"]):
                st.write(msg["text"])
                st.caption(msg["timestamp"])

    # --- Tab 2: Bot Arena ---
    with tab2:
        st.subheader("🤖 Bot Arena")
        # Two bots talk to each other in real-time, user observes only
        bot_names = list(BOT_PROFILES.keys())
        bot1, bot2 = bot_names[0], bot_names[1]

        def get_bot_arena_history():
            history = redis_client.lrange(BOT_ARENA_HISTORY_KEY, 0, -1)
            return [json.loads(item) for item in history if item]

        def add_bot_arena_message(sender, text):
            msg_data = {
                "sender": sender,
                "text": text,
                "is_bot": True,
                "timestamp": str(datetime.now())
            }
            redis_client.rpush(BOT_ARENA_HISTORY_KEY, json.dumps(msg_data))
            redis_client.ltrim(BOT_ARENA_HISTORY_KEY, -BOT_ARENA_MAX_MSGS, -1)

        # Generate next bot message if needed
        history = get_bot_arena_history()
        BOT_ARENA_DELAY_SECONDS = 15  # Increase delay to slow down bot conversation
        if not history:
            # Start the conversation
            add_bot_arena_message(bot1, "Hello, let's start a conversation!")
            st.rerun()
        else:
            last_msg = history[-1]
            # Alternate bots
            next_bot = bot2 if last_msg["sender"] == bot1 else bot1
            # Only generate if last message is older than BOT_ARENA_DELAY_SECONDS
            last_time = datetime.fromisoformat(last_msg["timestamp"])
            if (datetime.now() - last_time).total_seconds() > BOT_ARENA_DELAY_SECONDS:
                # Compose prompt
                system_prompt = BOT_PROFILES[next_bot]["system_prompt"]
                temperature = BOT_PROFILES[next_bot]["temperature"]
                # Build conversation for context
                messages = [
                    {"role": "system", "content": system_prompt}
                ]
                for msg in history[-6:]:
                    role = "assistant" if msg["sender"] == next_bot else "user"
                    messages.append({"role": role, "content": msg["text"]})
                # Get bot response
                try:
                    response = ai_client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=messages,
                        temperature=temperature,
                        max_tokens=80,
                    )
                    bot_reply = response.choices[0].message.content.strip()
                except Exception as e:
                    bot_reply = f"(Error: {e})"
                add_bot_arena_message(next_bot, bot_reply)
                st.rerun()

        # Display bot conversation (newest at top)
        st.info("You are observing the bots. You cannot send messages in this arena.")
        arena_history = get_bot_arena_history()[-20:]
        for msg in reversed(arena_history):  # Reverse to show newest at top
            avatar = BOT_PROFILES.get(msg["sender"], {}).get("avatar", "🤖")
            with st.chat_message(msg["sender"], avatar=avatar):
                st.write(msg["text"])
                st.caption(msg["timestamp"])

    # --- Tab 3: User & Multi-Bot Chat ---
    with tab3:
        st.subheader("👥🤖 User & Multi-Bot Chat")

        MULTIBOT_BOTS = [
            {"name": name, "profile": profile}
            for name, profile in BOT_PROFILES.items()
        ]

        def get_multibot_history():
            history = redis_client.lrange(MULTIBOT_HISTORY_KEY, 0, -1)
            return [json.loads(item) for item in history if item]

        def save_multibot_message(msg):
            # Add timestamp if not present
            if "timestamp" not in msg:
                msg["timestamp"] = str(datetime.now())
            redis_client.rpush(MULTIBOT_HISTORY_KEY, json.dumps(msg))
            # Keep only the last N*2+2 messages (system + N turns)
            redis_client.ltrim(MULTIBOT_HISTORY_KEY, -((MULTIBOT_MAX_TURNS*2)+2), -1)

        def trim_history(history, max_msgs=MULTIBOT_HISTORY_COUNT):
            return history[-max_msgs:] if len(history) > max_msgs else history

        # --- UI: select bots via checkboxes ---
        bot_names = [bot["name"] for bot in MULTIBOT_BOTS]
        selected_bots = st.multiselect(
            "Choose which bots will participate in the conversation:",
            options=bot_names,
            default=bot_names.copy(),
            key="selected_bots"
        )

        # --- Delete history button ---
        delete_clicked = st.button("🗑️ Delete Chat History")
        if delete_clicked:
            redis_client.delete(MULTIBOT_HISTORY_KEY)  # remove chat history from Redis
            st.success("Chat history deleted.")
            multibot_history = []  # clear history in current run
        else:
            multibot_history = get_multibot_history()

        user_input = st.chat_input("Enter your message for the bots...")

        # Show full chat history (newest at top)
        for msg in reversed(multibot_history):
            if msg["role"] == "system":
                continue
            timestamp = msg.get("timestamp", "")
            if msg["role"] == "user":
                with st.chat_message(msg["sender"]):
                    st.write(msg["content"])
                    st.caption(timestamp)
            elif msg["role"] == "assistant":
                bot_name = msg.get("bot", "Bot")
                avatar = BOT_PROFILES.get(bot_name, {}).get("avatar", "🤖")
                with st.chat_message(bot_name, avatar=avatar):
                    st.write(msg["content"])
                    st.caption(timestamp)

        # --- Handle user input ---
        if user_input:
            # 1. Add user message to history (redis)
            save_multibot_message({"role": "user", "sender": st.session_state.user, "content": user_input})
            multibot_history = get_multibot_history()

            # 2. Decide which bot(s) should reply
            bot_to_reply = [bot for bot in MULTIBOT_BOTS if bot["name"] in selected_bots]

            # If no bots are selected, do not generate AI response
            if not bot_to_reply:
                pass
            else:
                # 3. For the chosen bots, generate reply and append (feed last 20 messages)
                trimmed = trim_history(multibot_history)
                messages = []
                for m in trimmed:
                    if m["role"] == "assistant":
                        messages.append({
                            "role": "assistant",
                            "content": m["content"],
                            "name": m.get("bot", "")
                        })
                    else:
                        messages.append({
                            "role": m["role"],
                            "content": m["content"]
                        })
                # Add system instruction for this bot's turn
                messages.append({
                    "role": "system",
                    "content": multibot_prompt(bot_to_reply)
                })
                try:
                    response = ai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=messages,
                        temperature=MULTIBOT_ANSWER_TEMPERATURE,
                    )
                    bot_reply = response.choices[0].message.content.strip()
                except Exception as e:
                    bot_reply = f"(Error: {e})"

                # Save bot answer to redis
                for reply in reversed(bot_reply.strip().split("\n\n")):
                    name, message = map(str, reply.split(":", 1))
                    save_multibot_message({
                        "role": "assistant",
                        "bot": name.strip(),
                        "content": message.strip()
                    })

            st.rerun()

        # Auto-refresh every 2 seconds
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
