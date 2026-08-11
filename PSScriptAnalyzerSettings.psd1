@{
    # Every default rule runs, and two are excluded with their reasons, matching how the shell gate takes shellcheck's defaults and disables a finding inline where the finding is wrong for the program being quoted.
    ExcludeRules = @(
        # The scripts under host-setup/windows write their report to the console as their whole purpose, mirroring the printf calls in their Linux siblings.
        # Write-Output is not merely a different spelling here: these functions return exit codes through the pipeline, so report text on the same stream would arrive at the caller as a return value.
        'PSAvoidUsingWriteHost'

        # Every script here already carries a preview and a consent step, in the shape its Linux sibling uses: -DryRun prints what would run, and a confirm prompt asks before the host changes.
        # Supporting ShouldProcess would add -WhatIf and -Confirm beside them, so a reader would face two spellings of preview and two of consent, one of them undocumented.
        # A function is named for what it does instead, and the rule is declined here rather than worked around by renaming setters into something they are not.
        'PSUseShouldProcessForStateChangingFunctions'
    )
}
