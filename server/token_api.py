from tokens import AccessToken, ServiceRtc


def generate_viewer_token(app_id, app_cert, channel, uid, expire_s=86400):
    """Generate an Agora v007 audience-only token for a viewer channel."""
    token = AccessToken(app_id, app_cert, expire=expire_s)
    rtc = ServiceRtc(channel, uid)
    rtc.add_privilege(ServiceRtc.kPrivilegeJoinChannel, expire_s)
    token.add_service(rtc)
    return token.build()
