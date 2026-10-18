from enum import Enum


class WorkOrderStatus(str, Enum):
    DRAFT = 'draft'
    ISSUED = 'issued'
    IN_PROGRESS = 'in_progress'
    SUBMITTED = 'submitted'
    ACCEPTED = 'accepted'
    CLOSED = 'closed'
    CANCELLED = 'cancelled'


STATUS_LABELS = dict(draft='Черновик', issued='Выдан', in_progress='В работе', submitted='Работы сданы',
                     accepted='Проверен', closed='Закрыт', cancelled='Отменён')
