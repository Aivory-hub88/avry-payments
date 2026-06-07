"""Data models for contact form"""
from pydantic import BaseModel, Field, EmailStr


class ContactForm(BaseModel):
    """Contact form submission"""
    name: str = Field(min_length=1, description="Contact name")
    company: str = Field(min_length=1, description="Company name")
    email: EmailStr = Field(description="Contact email address")
    message: str = Field(min_length=10, description="What do you want to build?")


class ContactResponse(BaseModel):
    """Response after contact form submission"""
    success: bool = Field(description="Whether submission was successful")
    message: str = Field(description="Response message")
