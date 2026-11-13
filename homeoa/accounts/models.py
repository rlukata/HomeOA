"""Identity: users, magic-link tokens, TOTP devices and recovery codes.

These are user-level, not association-scoped: one person signs in once and may hold roles
in several associations. Association membership lives in ``access``.
"""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeoa.core.crypto import EncryptedTextField


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)


class User(AbstractBaseUser, PermissionsMixin):
    """A person who can sign in. Keyed by email; passwordless by default."""

    email = models.EmailField(_("email address"), unique=True)
    full_name = models.CharField(_("full name"), max_length=200, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff"), default=False)
    date_joined = models.DateTimeField(_("date joined"), default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        return self.email

    @property
    def has_mfa(self) -> bool:
        device = TOTPDevice.objects.filter(user=self, confirmed_at__isnull=False)
        return device.exists() or self.passkeys.exists()

    @property
    def has_totp(self) -> bool:
        return TOTPDevice.objects.filter(user=self, confirmed_at__isnull=False).exists()


class MagicLinkToken(models.Model):
    """A single-use sign-in link. Only the SHA-256 of the token is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    request_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = _("magic link")
        verbose_name_plural = _("magic links")

    def __str__(self) -> str:
        return f"magic link for {self.user_id} at {self.created_at:%Y-%m-%d %H:%M}"


class LoginRequest(models.Model):
    """One row per login-form submission, known email or not; feeds the rate limit."""

    email = models.CharField(max_length=254, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self) -> str:
        return f"{self.email} from {self.ip}"


class TOTPDevice(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="totp_device")
    secret = EncryptedTextField()  # base32 TOTP seed, encrypted at rest (OPS-04)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"TOTP for {self.user_id}"


class RecoveryCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"recovery code for {self.user_id}"


class PasskeyCredential(models.Model):
    """A WebAuthn credential (passkey). Only the public key is stored (PLT-02)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="passkeys")
    name = models.CharField(_("name"), max_length=100)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.BigIntegerField(default=0)
    transports = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("passkey")
        verbose_name_plural = _("passkeys")
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"passkey {self.name} for {self.user_id}"
