"""Bonus features: pull requests on a local Gitea, richer dashboard data."""


def attach(rt) -> None:
    from iow.bonus.gitea import GiteaPublisher
    if rt.loop is not None:
        rt.loop.after_heal.append(GiteaPublisher(rt.config).publish)
