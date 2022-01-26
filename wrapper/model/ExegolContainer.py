import base64
import os
import shutil
from typing import Optional, Dict, Sequence

from docker.errors import NotFound
from docker.models.containers import Container
from rich.prompt import Confirm

from wrapper.console.cli.ParametersManager import ParametersManager
from wrapper.model.ContainerConfig import ContainerConfig
from wrapper.model.ExegolContainerTemplate import ExegolContainerTemplate
from wrapper.model.ExegolImage import ExegolImage
from wrapper.model.SelectableInterface import SelectableInterface
from wrapper.utils.ExeLog import logger, console


# Class of an existing exegol container
class ExegolContainer(ExegolContainerTemplate, SelectableInterface):

    def __init__(self, docker_container: Container, model: Optional[ExegolContainerTemplate] = None):
        logger.debug(f"== Loading container : {docker_container.name}")
        self.__container: Container = docker_container
        self.__id: str = docker_container.id
        if model is None:
            # Create Exegol container from an existing docker container
            super().__init__(docker_container.name,
                             config=ContainerConfig(docker_container),
                             image=ExegolImage(docker_image=docker_container.image))
            self.image.syncContainer(docker_container)
        else:
            # Create Exegol container from a newly created docker container with his object template.
            super().__init__(docker_container.name,
                             config=ContainerConfig(docker_container),
                             # Rebuild config from docker object to update workspace path
                             image=model.image)

    def __str__(self):
        """Default object text formatter, debug only"""
        return f"{self.getRawStatus()} - {super().__str__()}"

    def __getState(self) -> Dict:
        """Technical getter of the container status dict"""
        self.__container.reload()
        return self.__container.attrs.get("State", {})

    def getRawStatus(self) -> str:
        """Raw text getter of the container status"""
        return self.__getState().get("Status", "unknown")

    def getTextStatus(self) -> str:
        """Formatted text getter of the container status"""
        status = self.getRawStatus().lower()
        if status == "unknown":
            return "[red]:question:[/red] Unknown"
        elif status == "exited":
            return ":stop_sign: [red]Stopped"
        elif status == "running":
            return "[green]:play_button: [green]Running"
        return status

    def isRunning(self) -> bool:
        """Check is the container is running. Return bool."""
        return self.getRawStatus() == "running"

    def getFullId(self) -> str:
        """Container's id getter"""
        return self.__id

    def getId(self) -> str:
        """Container's short id getter"""
        return self.__container.short_id

    def getKey(self) -> str:
        """Universal unique key getter (from SelectableInterface)"""
        return self.name

    def start(self):
        """Start the docker container"""
        if not self.isRunning():
            logger.info(f"Starting container {self.name}")
            self.__container.start()

    def stop(self, timeout: int = 10):
        """Stop the docker container"""
        if self.isRunning():
            logger.info(f"Stopping container {self.name}")
            with console.status(f"Waiting to stop ({timeout}s timeout)", spinner_style="blue") as status:
                self.__container.stop(timeout=timeout)

    def spawnShell(self):
        """Spawn a shell on the docker container"""
        logger.info(f"Location of the exegol workspace on the host : {self.config.getHostWorkspacePath()}")
        for device in self.config.getDevices():
            logger.info(f"Shared host device: {device.split(':')[0]}")
        logger.success(f"Opening shell in Exegol '{self.name}'")
        # If GUI enable, allow X11 access on host ACL
        if self.config.isGUIEnable():
            logger.debug(f"Adding xhost ACL to local:{self.hostname}")
            os.system(f"xhost +local:{self.hostname} > /dev/null")
        # Using system command to attach the shell to the user terminal (stdin / stdout / stderr)
        os.system("docker exec -ti {} {}".format(self.getFullId(), ParametersManager().shell))
        # Docker SDK dont support (yet) stdin properly
        # result = self.__container.exec_run(ParametersManager().shell, stdout=True, stderr=True, stdin=True, tty=True)
        # logger.debug(result)

    def exec(self, command: Sequence[str], as_daemon: bool = True):
        """Execute a command / process on the docker container"""
        if not self.isRunning():
            self.start()
        logger.info("Executing command on Exegol")
        if logger.getEffectiveLevel() > logger.VERBOSE:
            logger.info("Hint: use verbose mode to see command output (-v).")
        cmd = self.formatShellCommand(command)
        stream = self.__container.exec_run(cmd, detach=as_daemon, stream=not as_daemon)
        if as_daemon:
            logger.success("Command successfully executed in background")
        else:
            try:
                # stream[0] : exit code
                # stream[1] : text stream
                for log in stream[1]:
                    logger.raw(log.decode("utf-8"))
                logger.success("End of the command")
            except KeyboardInterrupt:
                logger.info("Detaching process logging")
                logger.warning("Exiting this command do NOT stop the process in the container")

    @staticmethod
    def formatShellCommand(command: Sequence[str]):
        """Generic method to format a shell command and support zsh aliases"""
        # Using base64 to escape special characters
        str_cmd = ' '.join(command)
        logger.success(f"Command received: {str_cmd}")
        cmd_b64 = base64.b64encode(str_cmd.encode('utf-8')).decode('utf-8')
        # Load zsh aliases and call eval to force aliases interpretation
        cmd = f'zsh -c "source /opt/.zsh_aliases; eval $(echo {cmd_b64} | base64 -d)"'
        logger.debug(f"Formatting zsh command: {cmd}")
        return cmd

    def remove(self):
        """Stop and remove the docker container"""
        self.stop(timeout=2)
        logger.info(f"Removing container {self.name}")
        try:
            self.__container.remove()
            logger.success(f"Container {self.name} successfully removed.")
        except NotFound:
            logger.error(
                f"The container {self.name} has already been removed (probably created as a temporary container).")
        self.__removeVolume()

    def __removeVolume(self):
        """Remove private workspace volume directory if exist"""
        volume_path = self.config.getPrivateVolumePath()
        if volume_path != '':
            logger.verbose("Removing workspace volume")
            logger.debug(f"Removing volume {volume_path}")
            try:
                if os.listdir(volume_path):
                    # Directory is not empty
                    if not Confirm.ask(
                            f"[blue][?][/blue] Workspace {volume_path} is not empty, do you want to delete it? [bright_magenta]\[y/N][/bright_magenta]",
                            show_choices=False,
                            show_default=False,
                            default=False):
                        # User can choose not to delete the workspace on the host
                        return
                shutil.rmtree(volume_path)
                logger.success("Private workspace volume removed successfully")
            except PermissionError:
                logger.warning(f"I don't have the rights to remove {volume_path} (do it yourself)")
            except Exception as err:
                logger.error(err)
        else:
            # Check if container workspace is a WSL volume or a custom one
            path = self.config.getHostWorkspacePath()
            if path.startswith('/wsl/') or path.startswith('\\wsl\\'):
                # Docker volume defines from WSL don't return the real path, they cannot be automatically removed
                logger.warning("Warning: WSL workspace directory cannot be removed automatically.")
