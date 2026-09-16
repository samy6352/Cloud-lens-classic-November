"""Offline quota and address-plan regressions learned from the MCAPS rehearsal."""
# cloudlens-test: requires-offline
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell required")
class PreflightChecks(unittest.TestCase):
    def test_quota_key_mapping_and_ipv4_ranges(self):
        script = r"""
. '__HELPER__'
if (-not (Test-CloudLensIpv4Overlap '10.70.0.0/16' '10.70.2.0/24')) { throw 'Overlap missed' }
if (Test-CloudLensIpv4Overlap '10.80.0.0/16' '10.70.0.0/16') { throw 'False overlap' }
if (Test-CloudLensIpv4Overlap '10.80.1.0/24' '10.80.2.0/24') { throw 'Adjacent ranges overlap' }
foreach($bad in @('10.80.1.1/24','999.0.0.0/16','10.0.0.0/33','::1/128')) {
    $failed=$false
    try { Get-CloudLensIpv4Range $bad | Out-Null } catch { $failed=$true }
    if (-not $failed) { throw "Accepted invalid CIDR $bad" }
}
$models=@(@{model=@{name='gpt-4.1-mini'; skus=@(
    @{name='GlobalStandard'; usageName='OpenAI.GlobalStandard.gpt4.1-mini'},
    @{name='GlobalStandard'; usageName='OpenAI.GlobalStandard.gpt4.1-mini-finetune'}
)}})
$usages=@(@{name=@{value='OpenAI.GlobalStandard.gpt4.1-mini'};currentValue=30;limit=5000},
          @{name=@{value='OpenAI.GlobalStandard.gpt4.1-mini-finetune'};currentValue=0;limit=1})
$q=Get-CloudLensModelQuota $models $usages 'gpt-4.1-mini' 'GlobalStandard' 30
if($q.Status -ne 'ok' -or $q.Remaining -ne 4970) { throw 'Wrong model quota key' }
if((Get-CloudLensModelQuota $models $usages 'gpt-4.1-mini' 'GlobalStandard' 5000).Status -ne 'blocked') { throw 'Overquota accepted' }
if((Get-CloudLensModelQuota $models @() 'gpt-4.1-mini' 'GlobalStandard' 30).Status -ne 'unknown') { throw 'Missing quota declared ready' }
Write-Output 'Quota and network checks passed'
"""
        script = script.replace("__HELPER__", str(ROOT / "infra" / "deployment-checks.ps1"))
        result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
