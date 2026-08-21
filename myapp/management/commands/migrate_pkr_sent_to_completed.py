from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from myapp.Models.Transaction_models import IncomingPayment, TransactionStatus, TransactionStatusHistory
from myapp.Models.Audit_models import AuditLog
from myapp.Utils.partner_ledger import distribute_fee_for_payment
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Safely migrates historical PKR_SENT payments to COMPLETED"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Run the migration in dry-run mode (no database changes).',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        
        # We need to find all PKR_SENT payments that might need migration.
        # We'll lock them row-by-row during processing.
        payments = IncomingPayment.objects.filter(status=TransactionStatus.PKR_SENT).prefetch_related(
            "covering_transfers", "outgoing_transfer"
        )
        
        migrated_count = 0
        skipped_count = 0
        failed_count = 0

        self.stdout.write(f"Found {payments.count()} payments currently in PKR_SENT status.")
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database changes will be made."))
            self.stdout.write("-" * 41)

        for payment in payments:
            try:
                # Check for evidence of a valid transfer before trying to lock
                valid_transfer = None
                for t in payment.covering_transfers.all():
                    if t.amount_pkr is not None:
                        valid_transfer = t
                        break
                
                if not valid_transfer and hasattr(payment, 'outgoing_transfer'):
                    t = payment.outgoing_transfer
                    if t is not None and getattr(t, 'amount_pkr', None) is not None:
                        valid_transfer = t
                
                if not valid_transfer:
                    self.stdout.write(self.style.WARNING(f"SKIPPED   {payment.reference}   No valid OutgoingPKRTransfer"))
                    skipped_count += 1
                    continue
                
                if dry_run:
                    migrated_count += 1
                    continue
                
                # Production execution: lock and re-validate
                with transaction.atomic():
                    locked_payment = IncomingPayment.objects.select_for_update().get(pk=payment.pk)
                    
                    if locked_payment.status != TransactionStatus.PKR_SENT:
                        self.stdout.write(self.style.WARNING(f"SKIPPED   {locked_payment.reference}   Status changed before lock"))
                        skipped_count += 1
                        continue
                        
                    # Re-resolve the related payout evidence from the database after locking
                    locked_valid_transfer = None
                    for t in locked_payment.covering_transfers.all():
                        if t.amount_pkr is not None:
                            locked_valid_transfer = t
                            break
                    
                    if not locked_valid_transfer and hasattr(locked_payment, 'outgoing_transfer'):
                        t = locked_payment.outgoing_transfer
                        if t is not None and getattr(t, 'amount_pkr', None) is not None:
                            locked_valid_transfer = t
                            
                    if not locked_valid_transfer:
                        self.stdout.write(self.style.WARNING(f"SKIPPED   {locked_payment.reference}   Transfer evidence missing after lock"))
                        skipped_count += 1
                        continue
                    
                    locked_payment.status = TransactionStatus.COMPLETED
                    locked_payment.completed_at = timezone.now()
                    locked_payment.is_stale = False
                    locked_payment.save(update_fields=["status", "completed_at", "is_stale", "updated_at"])

                    # Re-create the status history record directly
                    TransactionStatusHistory.objects.create(
                        payment=locked_payment,
                        from_status=TransactionStatus.PKR_SENT,
                        to_status=TransactionStatus.COMPLETED,
                        changed_by=None,
                        note="Historical payment migrated to simplified completion lifecycle",
                    )

                    # Distribute commission atomically
                    distribute_fee_for_payment(locked_payment)

                    # Record an audit log for this migration
                    AuditLog.record(
                        user=None, # System migration
                        action=AuditLog.ACTION_UPDATE,
                        target=locked_payment,
                        description=(
                            f"{locked_payment.reference}: Historical lifecycle migration: "
                            "PKR_SENT → COMPLETED. Valid outgoing PKR transfer was found. "
                            "Customer confirmation was not required under the new completion policy."
                        ),
                    )
                    
                    migrated_count += 1
            
            except Exception as e:
                logger.exception(f"Failed to migrate payment {payment.reference}")
                self.stdout.write(self.style.ERROR(f"FAILED    {payment.reference}   {str(e)}"))
                failed_count += 1
                continue

        self.stdout.write("-" * 41)
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"DRY RUN COMPLETE: Would migrate {migrated_count}, skip {skipped_count}, fail {failed_count}."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"MIGRATION COMPLETE: Migrated: {migrated_count} | Skipped: {skipped_count} | Failed: {failed_count}"
            ))
