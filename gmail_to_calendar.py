#!/usr/bin/env python3
"""
Gmail to Google Calendar automation
Scans recent emails, uses Claude to extract tasks, creates calendar events
"""

import os
import json
import base64
from datetime import datetime, timedelta
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from google.apps import calendar_v3, gmail_v1
from googleapiclient.discovery import build
import anthropic

# Gmail API scopes
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
CALENDAR_SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_gmail_service():
    """Authenticate and return Gmail service"""
    creds = None
    
    # Load token from environment or file
    if os.environ.get('GMAIL_TOKEN'):
        creds_data = json.loads(base64.b64decode(os.environ.get('GMAIL_TOKEN')))
        creds = Credentials.from_authorized_user_info(creds_data, GMAIL_SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print("Gmail token expired and couldn't refresh. Re-authenticate needed.")
                return None
        else:
            print("Gmail credentials not found. Manual auth required on first run.")
            return None
    
    return build('gmail', 'v1', credentials=creds)

def get_calendar_service():
    """Authenticate and return Google Calendar service"""
    creds = None
    
    # Load token from environment or file
    if os.environ.get('CALENDAR_TOKEN'):
        creds_data = json.loads(base64.b64decode(os.environ.get('CALENDAR_TOKEN')))
        creds = Credentials.from_authorized_user_info(creds_data, CALENDAR_SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print("Calendar token expired and couldn't refresh. Re-authenticate needed.")
                return None
        else:
            print("Calendar credentials not found. Manual auth required on first run.")
            return None
    
    return build('calendar', 'v3', credentials=creds)

def get_recent_emails(service, max_results=5):
    """Fetch recent unread emails from Gmail that haven't been processed"""
    try:
        # Only get unread emails that don't have the automation-processed label
        results = service.users().messages().list(
            userId='me',
            q='is:unread -label:AutomationProcessed',
            maxResults=max_results
        ).execute()
        
        messages = results.get('messages', [])
        emails = []
        
        for message in messages:
            msg = service.users().messages().get(userId='me', id=message['id']).execute()
            headers = msg['payload']['headers']
            
            email_data = {
                'id': message['id'],
                'from': next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown'),
                'subject': next((h['value'] for h in headers if h['name'] == 'Subject'), 'No subject'),
                'date': next((h['value'] for h in headers if h['name'] == 'Date'), ''),
            }
            
            # Get email body
            if 'parts' in msg['payload']:
                for part in msg['payload']['parts']:
                    if part['mimeType'] == 'text/plain':
                        data = part['body'].get('data', '')
                        if data:
                            email_data['body'] = base64.urlsafe_b64decode(data).decode()
                        break
            else:
                data = msg['payload']['body'].get('data', '')
                if data:
                    email_data['body'] = base64.urlsafe_b64decode(data).decode()
            
            emails.append(email_data)
        
        return emails
    except Exception as e:
        print(f"Error fetching emails: {e}")
        return []

def analyze_emails_for_tasks(emails):
    """Use Claude to extract actionable tasks from emails"""
    if not emails:
        return []
    
    client = anthropic.Anthropic(api_key=os.environ.get('CLAUDE_API_KEY'))
    
    # Prepare email content for Claude
    email_text = "\n\n".join([
        f"From: {e['from']}\nSubject: {e['subject']}\nBody: {e.get('body', '')[:500]}"
        for e in emails
    ])
    
    prompt = f"""Analyze these emails and extract ONLY actionable tasks that need to be added to a calendar.

For each task, return JSON with:
- title: Clear action item (e.g., "Review Q4 budget proposal")
- due_in_days: Number of days until due (0 for today, 1 for tomorrow, etc.)
- time: Suggested time in HH:MM format (e.g., "10:00" or "14:30")
- priority: "high", "medium", or "low"

Return only a JSON array. If no tasks found, return empty array [].

Emails:
{email_text}

Return ONLY valid JSON, no other text."""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1000,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    try:
        response_text = message.content[0].text.strip()
        # Remove markdown code blocks if present
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
        
        tasks = json.loads(response_text)
        return tasks if isinstance(tasks, list) else []
    except (json.JSONDecodeError, IndexError, AttributeError) as e:
        print(f"Error parsing Claude response: {e}")
        return []

def create_calendar_event(service, task):
    """Create a calendar event for a task"""
    try:
        # Calculate event date
        event_date = datetime.now() + timedelta(days=task.get('due_in_days', 0))
        
        # Parse time
        time_str = task.get('time', '09:00')
        hour, minute = map(int, time_str.split(':'))
        
        start_time = event_date.replace(hour=hour, minute=minute)
        end_time = start_time + timedelta(hours=1)
        
        event = {
            'summary': task['title'],
            'description': f"Task from email analysis (Priority: {task.get('priority', 'medium')})",
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'UTC',
            },
        }
        
        result = service.events().insert(calendarId='primary', body=event).execute()
        print(f"Event created: {task['title']}")
        return result
    except Exception as e:
        print(f"Error creating event: {e}")
        return None

def create_label_if_needed(service):
    """Create AutomationProcessed label if it doesn't exist"""
    try:
        labels = service.users().labels().list(userId='me').execute().get('labels', [])
        for label in labels:
            if label['name'] == 'AutomationProcessed':
                return label['id']
        
        # Label doesn't exist, create it
        label_body = {
            'name': 'AutomationProcessed',
            'labelListVisibility': 'labelShow',
            'messageListVisibility': 'hide'
        }
        result = service.users().labels().create(userId='me', body=label_body).execute()
        print(f"Created label: AutomationProcessed")
        return result['id']
    except Exception as e:
        print(f"Error managing labels: {e}")
        return None

def mark_emails_processed(service, email_ids):
    """Mark processed emails with AutomationProcessed label to prevent reprocessing"""
    label_id = create_label_if_needed(service)
    if not label_id:
        print("Warning: Could not create/find AutomationProcessed label")
        return
    
    for email_id in email_ids:
        try:
            service.users().messages().modify(
                userId='me',
                id=email_id,
                body={'addLabelIds': [label_id]}
            ).execute()
        except Exception as e:
            print(f"Error labeling email: {e}")

def main():
    print("Starting Gmail to Calendar automation...")
    
    # Check for required environment variables
    if not os.environ.get('CLAUDE_API_KEY'):
        print("Error: CLAUDE_API_KEY not set")
        return
    
    # Get services
    gmail_service = get_gmail_service()
    calendar_service = get_calendar_service()
    
    if not gmail_service or not calendar_service:
        print("Error: Could not authenticate with Gmail or Calendar")
        return
    
    # Get recent emails
    emails = get_recent_emails(gmail_service, max_results=5)
    if not emails:
        print("No unread emails found")
        return
    
    print(f"Found {len(emails)} unread emails")
    
    # Analyze with Claude
    tasks = analyze_emails_for_tasks(emails)
    print(f"Extracted {len(tasks)} tasks")
    
    # Create calendar events
    created_count = 0
    for task in tasks:
        if create_calendar_event(calendar_service, task):
            created_count += 1
    
    # Mark emails as processed to prevent re-analyzing them
    email_ids = [e['id'] for e in emails]
    mark_emails_processed(gmail_service, email_ids)
    
    print(f"Created {created_count} calendar events")
    print("Automation complete!")

if __name__ == '__main__':
    main()
