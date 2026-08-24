from sqlalchemy import create_engine

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
                                                                     
    echo=settings.SQL_ECHO,
    future=True,
                                                                    
    pool_pre_ping=True,
                                                                   
    pool_size=settings.DB_POOL_SIZE,
                                                                      
    max_overflow=settings.DB_MAX_OVERFLOW,
                                                                      
    pool_timeout=settings.DB_POOL_TIMEOUT,
                                                                       
    pool_recycle=settings.DB_POOL_RECYCLE,
                                                                 
    connect_args={
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
                                                                     
        "connect_timeout": 10,
    },
)