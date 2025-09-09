"""
Business logic models for schedule management.

These models represent the core business entities and logic,
separate from API serialization concerns.
"""

from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, ClassVar
from datetime import datetime, time, timedelta
import re


class WorkHours(BaseModel):
    """
    Business model for work hours.
    
    Handles validation and business logic for regular work hours.
    """

    start: str = Field(..., description="Start time in HH:MM format")
    end: str = Field(..., description="End time in HH:MM format")
    
    @validator('start', 'end')
    def validate_time_format(cls, v):
        """Validate time format is HH:MM"""
        if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', v):
            raise ValueError('Time must be in HH:MM format')
        return v
    
    @validator('end')
    def end_after_start(cls, v, values):
        """Ensure end time is after start time"""
        if 'start' in values:
            start_time = datetime.strptime(values['start'], '%H:%M').time()
            end_time = datetime.strptime(v, '%H:%M').time()
            if end_time <= start_time:
                raise ValueError('End time must be after start time')
        return v
    
    def duration_minutes(self) -> int:
        """Calculate work duration in minutes"""
        start_time = datetime.strptime(self.start, '%H:%M')
        end_time = datetime.strptime(self.end, '%H:%M')
        return int((end_time - start_time).total_seconds() / 60)


class Break(BaseModel):
    """
    Business model for break periods.
    
    Handles validation and business logic for work breaks.
    """

    start: str = Field(..., description="Break start time in HH:MM format")
    duration_minutes: int = Field(..., description="Break duration in minutes")
    
    @validator('start')
    def validate_time_format(cls, v):
        """Validate time format is HH:MM"""
        if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', v):
            raise ValueError('Time must be in HH:MM format')
        return v
    
    @validator('duration_minutes')
    def validate_duration(cls, v):
        """Ensure break duration is reasonable"""
        if v < 5 or v > 480:  # 5 minutes to 8 hours
            raise ValueError('Break duration must be between 5 and 480 minutes')
        return v
    
    def end_time(self) -> str:
        """Calculate break end time"""
        start_time = datetime.strptime(self.start, '%H:%M')
        end_time = start_time + timedelta(minutes=self.duration_minutes)
        return end_time.strftime('%H:%M')


class ExtraHour(BaseModel):
    """
    Business model for extra work hours.
    
    Represents additional work periods beyond regular hours.
    """
    
    start: str = Field(..., description="Extra hour start time in HH:MM format")
    end: str = Field(..., description="Extra hour end time in HH:MM format")
    
    @validator('start', 'end')
    def validate_time_format(cls, v):
        """Validate time format is HH:MM"""
        if not re.match(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$', v):
            raise ValueError('Time must be in HH:MM format')
        return v
    
    @validator('end')
    def end_after_start(cls, v, values):
        """Ensure end time is after start time"""
        if 'start' in values:
            start_time = datetime.strptime(values['start'], '%H:%M').time()
            end_time = datetime.strptime(v, '%H:%M').time()
            if end_time <= start_time:
                raise ValueError('End time must be after start time')
        return v
    
    def duration_minutes(self) -> int:
        """Calculate extra hour duration in minutes"""
        start_time = datetime.strptime(self.start, '%H:%M')
        end_time = datetime.strptime(self.end, '%H:%M')
        return int((end_time - start_time).total_seconds() / 60)


class Schedule(BaseModel):
    """
    Core business model for work schedules.
    
    Represents the complete schedule configuration with business logic
    for validation and calculations.
    """
    
    VALID_DAYS: ClassVar[List[str]] = [
        'monday', 'tuesday', 'wednesday', 'thursday', 
        'friday', 'saturday', 'sunday'
    ]

    active_days: List[str] = Field(..., description="List of active work days")
    work_hours: WorkHours = Field(..., description="Regular work hours")
    break_time: Break = Field(..., description="Break configuration")
    
    @validator('active_days')
    def validate_days(cls, v):
        """Validate active days are valid weekdays"""
        if not v:
            raise ValueError('At least one active day is required')
        
        invalid_days = [day for day in v if day.lower() not in cls.VALID_DAYS]
        if invalid_days:
            raise ValueError(f'Invalid days: {invalid_days}')
        
        return [day.lower() for day in v]
    
    @validator('break_time')
    def break_within_work_hours(cls, v, values):
        """Ensure break time is within work hours"""
        if 'work_hours' in values:
            work_hours = values['work_hours']
            break_start = datetime.strptime(v.start, '%H:%M').time()
            work_start = datetime.strptime(work_hours.start, '%H:%M').time()
            work_end = datetime.strptime(work_hours.end, '%H:%M').time()
            
            if not (work_start <= break_start <= work_end):
                raise ValueError('Break must be within work hours')
        
        return v
    
    def total_work_minutes(self) -> int:
        """Calculate total work minutes excluding break"""
        return self.work_hours.duration_minutes() - self.break_time.duration_minutes
    
    def is_work_day(self, day: str) -> bool:
        """Check if a given day is a work day"""
        return day.lower() in self.active_days


class ScheduleEntity(BaseModel):
    """
    Complete business entity for a device schedule.
    
    Includes all schedule information plus metadata and extra hours.
    """
    
    id: Optional[int] = None
    device_name: str = Field(..., description="Device identifier")
    schedule: Schedule = Field(..., description="Basic schedule configuration")
    extra_hours: Optional[Dict[str, List[ExtraHour]]] = Field(
        None, description="Extra hours by day of week"
    )
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    version: str = Field(default="1.0", description="Schedule version")
    source: str = Field(default="api", description="Schedule source")
    
    @validator('device_name')
    def validate_device_name(cls, v):
        """Validate device name is not empty"""
        if not v or not v.strip():
            raise ValueError('Device name cannot be empty')
        return v.strip()
    
    @validator('extra_hours')
    def validate_extra_hours(cls, v):
        """Validate extra hours days are valid"""
        if v:
            valid_days = Schedule.VALID_DAYS
            invalid_days = [day for day in v.keys() if day.lower() not in valid_days]
            if invalid_days:
                raise ValueError(f'Invalid extra hour days: {invalid_days}')
        return v
    
    def get_total_work_minutes_for_day(self, day: str) -> int:
        """Calculate total work minutes for a specific day including extra hours"""
        if not self.schedule.is_work_day(day):
            return 0
        
        total_minutes = self.schedule.total_work_minutes()
        
        # Add extra hours for the day
        if self.extra_hours and day.lower() in self.extra_hours:
            for extra_hour in self.extra_hours[day.lower()]:
                total_minutes += extra_hour.duration_minutes()
        
        return total_minutes
    
    def get_weekly_work_minutes(self) -> int:
        """Calculate total work minutes for the week"""
        total = 0
        for day in Schedule.VALID_DAYS:
            total += self.get_total_work_minutes_for_day(day)
        return total