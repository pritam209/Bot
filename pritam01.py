from turtle import delay
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import asyncio
from datetime import datetime, timedelta
import json


# Google Sheets API setup
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SERVICE_ACCOUNT_FILE = 'creds.json'  # Replace with your service account file path


# Authenticate the Google Sheets client once globally
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
client = gspread.authorize(creds)


# Telegram Bot Token
TELEGRAM_TOKEN = "7074557320:AAGIpnP-2VE5sUfjOiScPAfyIDUOq4lOcRQ"  # Replace with your bot token


# Global variables for queue and user state management
lead_queue = []  # FIFO queue for /getnewlead requests
user_states = {}  # Track user verification status and current lead
pending_leads = {}  # Track assigned leads waiting for status
lead_assignments = {}  # Track lead assignments with timestamps


# User states
STATE_UNVERIFIED = "unverified"
STATE_VERIFIED = "verified"
STATE_WAITING_PHONE = "waiting_phone"
STATE_PENDING_LEAD_STATUS = "pending_lead_status"


async def log_audit(user_id, username, action, lead_id=None, details=None):
    """Log user actions to Audit Trails worksheet"""
    try:
        # Open the MetaLeadsData sheet and get the 'audit trails' tab
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('audit trails')
        
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Format user info
        user_info = f"{username}({user_id})" if username else str(user_id)
        
        # Prepare row data
        row_data = [
            user_info,
            action,
            str(lead_id) if lead_id else "",
            timestamp,
            details or ""
        ]
        
        # Check if headers exist, if not create them
        try:
            headers = sheet.row_values(1)
            if not headers or len(headers) == 0:
                sheet.update('A1:E1', [['User', 'Action', 'Lead ID', 'Timestamp', 'Details']])
        except:
            sheet.update('A1:E1', [['User', 'Action', 'Lead ID', 'Timestamp', 'Details']])
        
        sheet.append_row(row_data)
        
    except Exception as e:
        print(f"Error logging audit: {e}")


async def get_next_lead():
    """Get the next available lead from leads worksheet"""
    try:
        # Open the MetaLeadsData sheet and get the 'leads' tab
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('leads')
        data = sheet.get_all_records()
        
        if not data:
            return None
        
        # Filter unassigned leads (Status is empty or not 'assigned')
        unassigned_leads = []
        for i, lead in enumerate(data):
            status = str(lead.get('Status', '')).lower().strip()
            if status == '' or status not in ['assigned', 'interested', 'not connected', 'call done - info given', 'think & let me know', 'call back', 'unresponsive']:
                unassigned_leads.append((i + 2, lead))  # i+2 for actual sheet row
        
        if not unassigned_leads:
            return None
        
        # Sort by LeadID (ascending order for FIFO)
        def priority_sort(lead_tuple):
            row_num, lead_data = lead_tuple
            lead_id = lead_data.get('LeadID', 0)
            try:
                return int(lead_id)
            except:
                return 999999  # Put invalid IDs at the end
        
        unassigned_leads.sort(key=priority_sort)
        return unassigned_leads[0] if unassigned_leads else None
        
    except Exception as e:
        print(f"Error getting next lead: {e}")
        return None


async def assign_lead_to_user(user_id, username):
    """Assign a lead to user and mark as assigned"""
    try:
        lead_info = await get_next_lead()
        if not lead_info:
            return None
        
        row_num, lead_data = lead_info
        
        # Update lead in the leads worksheet
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('leads')
        headers = sheet.row_values(1)
        
        # Find AssignedTo column
        try:
            assigned_col = headers.index('AssignedTo') + 1
        except ValueError:
            print("AssignedTo column not found in leads worksheet")
            return None
        
        # Find Status column
        try:
            status_col = headers.index('Status') + 1
        except ValueError:
            print("Status column not found in leads worksheet")
            return None
        
        # Update the lead
        user_name = user_states.get(user_id, {}).get('name', username if username else str(user_id))
        user_phone = user_states.get(user_id, {}).get('phone', '')
        user_display = f"{user_name} ({user_phone})" if user_phone else user_name
        sheet.update_cell(row_num, assigned_col, user_display)
        sheet.update_cell(row_num, status_col, 'assigned')
        
        # Track assignment
        lead_id = lead_data.get('LeadID', f"lead_{row_num}")
        pending_leads[user_id] = {
            'lead_id': lead_id,
            'row_num': row_num,
            'assigned_time': datetime.now(),
            'lead_data': lead_data
        }
        
        lead_assignments[user_id] = {
            'lead_id': lead_id,
            'assigned_time': datetime.now()
        }
        
        await log_audit(user_id, username, "Lead Assigned", lead_id, f"Lead assigned to user")
        
        return lead_data
        
    except Exception as e:
        print(f"Error assigning lead: {e}")
        return None


async def verify_user_phone(phone_number):
    """Verify if phone number exists in team worksheet"""
    try:
        # Open the MetaLeadsData sheet and get the 'team' tab
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('team')
        data = sheet.get_all_records()
        
        # Clean phone number (remove spaces, +, etc.)
        clean_phone = ''.join(filter(str.isdigit, str(phone_number)))
        
        for row in data:
            db_phone = ''.join(filter(str.isdigit, str(row.get('Phone Number', ''))))
            if clean_phone == db_phone:
                return True, row.get('Telegram Name', 'Unknown')
        
        return False, None
        
    except Exception as e:
        print(f"Error verifying phone: {e}")
        return False, None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    await log_audit(user_id, username, "Bot Started")
    
    # Check if user is already verified
    if user_id in user_states and user_states[user_id]['state'] == STATE_VERIFIED:
        await update.message.reply_text(
            f"Welcome back, {user_states[user_id]['name']}!\n\n"
            "Available commands:\n"
            "/getnewlead - Request a new lead\n"
            "/report - View your performance report\n"
            "/help - Show all commands"
        )
        return
    
    # For new users, request phone verification
    user_states[user_id] = {'state': STATE_WAITING_PHONE}
    
    # Create phone sharing button
    keyboard = [[KeyboardButton("📱 Share Phone Number", request_contact=True)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    
    await update.message.reply_text(
        "🔐 **Team Verification Required**\n\n"
        "Please share your phone number to verify you're part of the team.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle phone number verification"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if user_id not in user_states or user_states[user_id]['state'] != STATE_WAITING_PHONE:
        return
    
    phone_number = update.message.contact.phone_number
    
    # Verify phone number
    is_verified, team_name = await verify_user_phone(phone_number)
    
    if is_verified:
        user_states[user_id] = {
            'state': STATE_VERIFIED,
            'phone': phone_number,
            'name': team_name
        }
        
        await log_audit(user_id, username, "User Verified", details=f"Phone: {phone_number}")
        
        await update.message.reply_text(
            f"✅ **Verification Successful!**\n\n"
            f"Welcome {team_name}!\n\n"
            "Available commands:\n"
            "/getnewlead - Request a new lead\n"
            "/report - View your performance report\n"
            "/help - Show all commands",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='Markdown'
        )
    else:
        await log_audit(user_id, username, "Verification Failed", details=f"Phone: {phone_number}")
        
        await update.message.reply_text(
            "❌ **Verification Failed**\n\n"
            "You are not part of the team. Please contact your manager.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode='Markdown'
        )


async def get_new_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /getnewlead command with queue system"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Check if user is verified
    if user_id not in user_states or user_states[user_id]['state'] != STATE_VERIFIED:
        await update.message.reply_text("❌ Please verify your phone number first using /start")
        return
    
    # Check if user has pending lead status
    if user_id in pending_leads:
        pending_lead = pending_leads[user_id]
        await update.message.reply_text(
            f"⚠️ **Please submit the status of your previous lead first!**\n\n"
            f"Lead ID: {pending_lead['lead_id']}\n"
            f"Assigned: {pending_lead['assigned_time'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "Use the status buttons to update the lead status."
        )
        return
    
    # Add user to queue if not already there
    if user_id not in [user['user_id'] for user in lead_queue]:
        queue_entry = {
            'user_id': user_id,
            'username': username,
            'name': user_states[user_id]['name'],
            'joined_time': datetime.now()
        }
        lead_queue.append(queue_entry)
        
        await log_audit(user_id, username, "Enqueued for lead", details=f"Queue position {len(lead_queue)}")
    
    # Process queue immediately if user is first
    if lead_queue and lead_queue[0]['user_id'] == user_id:
        await process_next_in_queue(context)
    else:
        # Find user position in queue
        position = next((i + 1 for i, user in enumerate(lead_queue) if user['user_id'] == user_id), 0)
        
        await update.message.reply_text(
            f"📋 **Added to Lead Queue**\n\n"
            f"Your Position: **{position}**\n"
            f"Estimated Wait: ~{position * 2} minutes\n\n"
            f"You'll be notified when it's your turn!",
            parse_mode='Markdown'
        )


async def set_lead_timeout(user_id, username, timeout_seconds):
    """Set timeout for lead status update"""
    await asyncio.sleep(timeout_seconds)
    if user_id in pending_leads:
        await mark_lead_unresponsive(user_id, username)


async def mark_lead_unresponsive(user_id, username):
    """Mark lead as unresponsive if user doesn't update status in time"""
    if user_id not in pending_leads:
        return
    
    try:
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('leads')
        pending_lead = pending_leads[user_id]
        row_num = pending_lead['row_num']
        
        headers = sheet.row_values(1)
        
        # Find Status column
        try:
            status_col = headers.index('Status') + 1
        except ValueError:
            print("Status column not found")
            return
        
        # Mark as unresponsive
        sheet.update_cell(row_num, status_col, 'Unresponsive')
        
        # Remove from pending leads
        del pending_leads[user_id]
        
        await log_audit(user_id, username, "Lead Timeout", pending_lead['lead_id'], "Marked as Unresponsive")
        
        print(f"⚠️ Lead timeout: User {username} ({user_id}) - Lead marked as Unresponsive")
        
    except Exception as e:
        print(f"Error marking lead unresponsive: {e}")


async def send_message_to_user(application, user_id, text, reply_markup=None):
    """Helper function to send message to specific user"""
    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        print(f"Error sending message to user {user_id}: {e}")


async def process_next_in_queue(context=None):
    """Process the next user in queue and assign lead"""
    global lead_queue
    
    if not lead_queue:
        return
    
    next_user = lead_queue.pop(0)
    user_id = next_user['user_id']
    username = next_user['username']
    name = next_user['name']
    
    # Assign lead
    lead_data = await assign_lead_to_user(user_id, username)
    
    if lead_data:
        # Format lead information
        lead_info = f"**🎯 New Lead Assigned!**\n\n"
        lead_info += f"**Lead ID:** {lead_data.get('LeadID', 'N/A')}\n"
        lead_info += f"**Name:** {lead_data.get('Name', 'N/A')}\n"
        lead_info += f"**Phone:** {lead_data.get('Phone', 'N/A')}\n"
        if lead_data.get('OtherInfo'):
            lead_info += f"**Other Info:** {lead_data.get('OtherInfo', 'N/A')}\n"
        
        # Create status buttons
        keyboard = [
            [InlineKeyboardButton("📞 Interested", callback_data=f"status_interested_{user_id}")],
            [InlineKeyboardButton("📵 Not Connected", callback_data=f"status_notconnected_{user_id}")],
            [InlineKeyboardButton("✅ Call Done - Info Given", callback_data=f"status_calldone_{user_id}")],
            [InlineKeyboardButton("🤔 Think & Let Me Know", callback_data=f"status_think_{user_id}")],
            [InlineKeyboardButton("🔄 Call Back", callback_data=f"status_callback_{user_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        lead_info += f"\n⏰ **Please update status within 15 minutes**"
        
        # Set timeout for 15 minutes
        asyncio.create_task(set_lead_timeout(user_id, username, 900))  # 15 minutes
        
        # Send message to user
        try:
            if context and context.application:
                await context.application.bot.send_message(
                    chat_id=user_id,
                    text=lead_info,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
        except Exception as e:
            print(f"Error sending lead to user {user_id}: {e}")


async def handle_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lead status update callbacks"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    username = update.effective_user.username
    callback_data = query.data
    
    if user_id not in pending_leads:
        await query.edit_message_text("❌ No pending lead found.")
        return
    
    # Extract status from callback
    status_map = {
        'interested': 'Interested',
        'notconnected': 'Not Connected',
        'calldone': 'Call Done - Info Given',
        'think': 'Think & Let Me Know',
        'callback': 'Call Back'
    }
    
    status_key = callback_data.split('_')[1]
    status_value = status_map.get(status_key, 'Unknown')
    
    # Update lead status in sheet
    try:
        spreadsheet = client.open('MetaLeadsData')
        sheet = spreadsheet.worksheet('leads')
        pending_lead = pending_leads[user_id]
        row_num = pending_lead['row_num']
        
        headers = sheet.row_values(1)
        
        # Find Status column
        try:
            status_col = headers.index('Status') + 1
        except ValueError:
            await query.edit_message_text("❌ Status column not found in leads worksheet.")
            return
        
        # Update status
        sheet.update_cell(row_num, status_col, status_value)
        
        # Remove from pending leads
        del pending_leads[user_id]
        
        await log_audit(user_id, username, "Lead Status Updated", pending_lead['lead_id'], status_value)
        
        await query.edit_message_text(
            f"✅ **Status Updated Successfully!**\n\n"
            f"Lead Status: **{status_value}**\n\n"
            f"Use /getnewlead to request another lead.",
            parse_mode='Markdown'
        )
        
        # Process next person in queue automatically
        if lead_queue:
            asyncio.create_task(process_next_in_queue(context))
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error updating status: {e}")


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate user performance report"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if user_id not in user_states or user_states[user_id]['state'] != STATE_VERIFIED:
        await update.message.reply_text("❌ Please verify your phone number first using /start")
        return
    
    try:
        # Get audit data for this user
        spreadsheet = client.open('MetaLeadsData')
        audit_sheet = spreadsheet.worksheet('audit trails')
        audit_data = audit_sheet.get_all_records()
        
        # Filter user actions
        user_identifier = f"{username}({user_id})" if username else str(user_id)
        user_actions = [row for row in audit_data if str(row.get('User', '')) == user_identifier]
        
        # Calculate stats
        leads_assigned = len([action for action in user_actions if action.get('Action') == 'Lead Assigned'])
        status_submitted = len([action for action in user_actions if action.get('Action') == 'Lead Status Updated'])
        
        # Get status breakdown
        status_actions = [action for action in user_actions if action.get('Action') == 'Lead Status Updated']
        status_breakdown = {}
        for action in status_actions:
            status = action.get('Details', 'Unknown')
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
        
        # Calculate success rate (Interested / Total)
        interested_count = status_breakdown.get('Interested', 0)
        success_rate = (interested_count / status_submitted * 100) if status_submitted > 0 else 0
        
        # Create report
        report = f"📊 **Your Performance Report**\n\n"
        report += f"**Name:** {user_states[user_id]['name']}\n"
        report += f"**Leads Assigned:** {leads_assigned}\n"
        report += f"**Status Submitted:** {status_submitted}\n"
        report += f"**Success Rate:** {success_rate:.1f}%\n\n"
        
        if status_breakdown:
            report += "**Status Breakdown:**\n"
            for status, count in status_breakdown.items():
                report += f"• {status}: {count}\n"
        
        await update.message.reply_text(report, parse_mode='Markdown')
        
        await log_audit(user_id, username, "Report Viewed")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating report: {e}")


async def queue_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current queue status"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if user_id not in user_states or user_states[user_id]['state'] != STATE_VERIFIED:
        await update.message.reply_text("❌ Please verify your phone number first using /start")
        return
    
    if not lead_queue:
        await update.message.reply_text("📋 **Queue is empty**\n\nUse /getnewlead to join the queue.")
        return
    
    queue_text = "📋 **Current Lead Queue**\n\n"
    
    for i, user in enumerate(lead_queue[:10], 1):  # Show top 10
        name = user.get('name', 'Unknown')
        wait_time = datetime.now() - user['joined_time']
        wait_minutes = int(wait_time.total_seconds() / 60)
        
        status_emoji = "🔥" if i <= 3 else "⏳"
        
        if user['user_id'] == user_id:
            queue_text += f"**{i}. {name} (You)** {status_emoji} - Waiting {wait_minutes}m\n"
        else:
            queue_text += f"{i}. {name} {status_emoji} - Waiting {wait_minutes}m\n"
    
    if len(lead_queue) > 10:
        queue_text += f"\n... and {len(lead_queue) - 10} more users\n"
    
    queue_text += f"\n**Total in Queue:** {len(lead_queue)}"
    
    await update.message.reply_text(queue_text, parse_mode='Markdown')
    await log_audit(user_id, username, "Queue Status Viewed")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information"""
    help_text = """
🤖 **Lead Management Bot Commands**

**Basic Commands:**
/start - Verify your phone number and get started
/help - Show this help message

**For Verified Users:**
/getnewlead - Join queue to get a new lead
/queuestatus - View current queue status
/report - View your performance report

**How it works:**
1. Share your phone number to verify team membership
2. Use /getnewlead to join the lead assignment queue
3. When assigned a lead, update its status within 15 minutes
4. Use /report to track your performance

**Lead Status Options:**
📞 Interested
📵 Not Connected  
✅ Call Done - Info Given
🤔 Think & Let Me Know
🔄 Call Back

**Queue System:**
- FIFO (First In, First Out) basis
- Leads assigned by LeadID order
- 15 minute timeout for status updates

Need help? Contact your manager.
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("getnewlead", get_new_lead))
    app.add_handler(CommandHandler("queuestatus", queue_status_command))
    app.add_handler(CommandHandler("report", report_command))
    
    # Callback handlers
    app.add_handler(CallbackQueryHandler(handle_status_callback, pattern="^status_"))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    
    print("🤖 Lead Management Bot Started!")
    print("\nFeatures:")
    print("✅ Team verification via phone number")
    print("✅ FIFO queue system for lead distribution")
    print("✅ Lead assignment by LeadID order")
    print("✅ Lead status tracking with buttons")
    print("✅ 15-minute timeout for status updates")
    print("✅ Performance reporting and analytics")
    print("✅ Audit trail logging")
    print("\nCommands:")
    print("- /start: Team verification and welcome")
    print("- /getnewlead: Join queue for lead assignment")
    print("- /queuestatus: View current queue status")
    print("- /report: Individual performance report")
    print("- /help: Show all commands")
    print("\nRequired Google Sheet: 'MetaLeadsData'")
    print("Required Worksheets:")
    print("- 'leads': Main lead database")
    print("- 'team': Team member verification")
    print("- 'audit trails': Action logging")
    
    app.run_polling()
